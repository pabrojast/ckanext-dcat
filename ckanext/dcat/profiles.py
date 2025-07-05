from builtins import str
from past.builtins import basestring
from builtins import object
import datetime
import json

from urllib.parse import quote

from dateutil.parser import parse as parse_date

from ckantoolkit import config
from ckantoolkit import url_for

import rdflib
from rdflib import URIRef, BNode, Literal
from rdflib.namespace import Namespace, RDF, XSD, SKOS, RDFS

from geomet import wkt, InvalidGeoJSONException

from ckan.model.license import LicenseRegister
from ckan.plugins import toolkit
from ckan.lib.munge import munge_tag
from ckanext.dcat.utils import resource_uri, publisher_uri_organization_fallback, DCAT_EXPOSE_SUBCATALOGS, DCAT_CLEAN_TAGS, get_langs
import re
import logging
from ckanext.schemingdcat.helpers import schemingdcat_get_default_lang

# Default values
from ckanext.dcat.profiles_default import metadata_field_names, spain_dcat_default_values, euro_dcat_ap_default_values, default_translated_fields, default_translated_fields_spain_dcat
from ckanext.dcat.codelists import load_codelists

DC = Namespace("http://purl.org/dc/elements/1.1/")
DCT = Namespace("http://purl.org/dc/terms/")
DCAT = Namespace("http://www.w3.org/ns/dcat#")
DCATAP = Namespace("http://data.europa.eu/r5r/")
ADMS = Namespace("http://www.w3.org/ns/adms#")
VCARD = Namespace("http://www.w3.org/2006/vcard/ns#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
SCHEMA = Namespace('http://schema.org/')
TIME = Namespace('http://www.w3.org/2006/time#')
LOCN = Namespace('http://www.w3.org/ns/locn#')
GSP = Namespace('http://www.opengis.net/ont/geosparql#')
OWL = Namespace('http://www.w3.org/2002/07/owl#')
SPDX = Namespace('http://spdx.org/rdf/terms#')
CNT = Namespace('http://www.w3.org/2011/content#')

GEOJSON_IMT = 'http://www.iana.org/assignments/media-types/application/vnd.geo+json'

# INSPIRE Codelists
codelists = load_codelists()
MD_INSPIRE_REGISTER = codelists['MD_INSPIRE_REGISTER']
MD_FORMAT = codelists['MD_FORMAT']
MD_ES_THEMES = codelists['MD_ES_THEMES']
MD_EU_THEMES = codelists['MD_EU_THEMES']
MD_EU_LANGUAGES = codelists['MD_EU_LANGUAGES']
MD_ES_FORMATS = codelists['MD_ES_FORMATS']

namespaces = {
    'dc': DC,
    'dct': DCT,
    'dcat': DCAT,
    'dcatap': DCATAP,
    'adms': ADMS,
    'vcard': VCARD,
    'foaf': FOAF,
    'schema': SCHEMA,
    'time': TIME,
    'skos': SKOS,
    'locn': LOCN,
    'gsp': GSP,
    'owl': OWL,
    'cnt': CNT,
    'spdx': SPDX,
}

PREFIX_MAILTO = u'mailto:'

DISTRIBUTION_LICENSE_FALLBACK_CONFIG = 'ckanext.dcat.resource.inherit.license'

DEFAULT_LANG = schemingdcat_get_default_lang()

log = logging.getLogger(__name__)

class URIRefOrLiteral(object):
    '''Helper which creates an URIRef if the value appears to be an http URL,
    or a Literal otherwise. URIRefs are also cleaned using CleanedURIRef.

    Like CleanedURIRef, this is a factory class.
    '''
    def __new__(cls, value):
        try:
            stripped_value = value.strip()
            if (isinstance(value, basestring) and (stripped_value.startswith("http://")
                                                or stripped_value.startswith("https://"))):
                uri_obj = CleanedURIRef(value)
                # although all invalid chars checked by rdflib should have been quoted, try to serialize
                # the object. If it breaks, use Literal instead.
                uri_obj.n3()
                # URI is fine, return the object
                return uri_obj
            else:
                return Literal(value)
        except Exception:
            # In case something goes wrong: use Literal
            return Literal(value)


class CleanedURIRef(object):
    '''Performs some basic URL encoding on value before creating an URIRef object.

    This is a factory for URIRef objects, which allows usage as type in graph.add()
    without affecting the resulting node types. That is,
    g.add(..., URIRef) and g.add(..., CleanedURIRef) will result in the exact same node type.
    '''
    @staticmethod
    def _careful_quote(value):
        # only encode this limited subset of characters to avoid more complex URL parsing
        # (e.g. valid ? in query string vs. ? as value).
        # can be applied multiple times, as encoded %xy is left untouched. Therefore, no
        # unquote is necessary beforehand.
        quotechars = ' !"$\'()*,;<>[]{|}\\^`'
        for c in quotechars:
            value = value.replace(c, quote(c))
        return value

    def __new__(cls, value):
        if isinstance(value, basestring):
            value = CleanedURIRef._careful_quote(value.strip())
        return URIRef(value)


class RDFProfile(object):
    '''Base class with helper methods for implementing RDF parsing profiles

       This class should not be used directly, but rather extended to create
       custom profiles
    '''

    def __init__(self, graph, compatibility_mode=False):
        '''Class constructor

        Graph is an rdflib.Graph instance.

        In compatibility mode, some fields are modified to maintain
        compatibility with previous versions of the ckanext-dcat parsers
        (eg adding the `dcat_` prefix or storing comma separated lists instead
        of JSON dumps).
        '''

        self.g = graph

        self.compatibility_mode = compatibility_mode

        # Cache for mappings of licenses URL/title to ID built when needed in
        # _license().
        self._licenceregister_cache = None

    def _datasets(self):
        '''
        Generator that returns all DCAT datasets on the graph

        Yields rdflib.term.URIRef objects that can be used on graph lookups
        and queries
        '''
        for dataset in self.g.subjects(RDF.type, DCAT.Dataset):
            yield dataset

    def _distributions(self, dataset):
        '''
        Generator that returns all DCAT distributions on a particular dataset

        Yields rdflib.term.URIRef objects that can be used on graph lookups
        and queries
        '''
        for distribution in self.g.objects(dataset, DCAT.distribution):
            yield distribution

    def _keywords(self, dataset_ref):
        '''
        Returns all DCAT keywords on a particular dataset
        '''
        keywords = self._object_value_list(dataset_ref, DCAT.keyword) or []
        # Split keywords with commas
        keywords_with_commas = [k for k in keywords if ',' in k]
        for keyword in keywords_with_commas:
            keywords.remove(keyword)
            keywords.extend([k.strip() for k in keyword.split(',')])

        return keywords
    
    def _themes(self, dataset_ref):
        '''
        Returns all DCAT themes on a particular dataset
        '''
        # Precompile regular expressions for faster matching
        data_es_pattern = re.compile(r"https?://datos\.gob\.es/")
        inspire_eu_pattern = re.compile(r"https?://inspire\.ec\.europa\.eu/theme")

        themes = set()

        for theme in self._object_value_list(dataset_ref, DCAT.theme):
            theme = theme.replace('https://', 'http://')

            if data_es_pattern.match(theme):
                themes.add(theme)
                if theme:
                    theme_es_dcat_ap = self._search_value_codelist(MD_ES_THEMES, theme, "id","dcat_ap") or None
                    themes.add(theme_es_dcat_ap)
                
            elif inspire_eu_pattern.match(theme):
                themes.add(theme)
                if theme:
                    theme_eu_dcat_ap = self._search_value_codelist(MD_EU_THEMES, theme, "id","dcat_ap") or None
                    themes.add(theme_eu_dcat_ap)
                    

        return themes

    def _object(self, subject, predicate):
        '''
        Helper for returning the first object for this subject and predicate

        Both subject and predicate must be rdflib URIRef or BNode objects

        Returns an rdflib reference (URIRef or BNode) or None if not found
        '''
        for _object in self.g.objects(subject, predicate):
            return _object
        return None

    def _object_value(self, subject, predicate, multilang=False):
        '''
        Given a subject and a predicate, returns the value of the object

        Both subject and predicate must be rdflib URIRef or BNode objects

        If found, the string representation is returned, else an empty string
        '''
        lang_dict = {}
        fallback = ''
        for o in self.g.objects(subject, predicate):
            if isinstance(o, Literal):
                if o.language and o.language == DEFAULT_LANG:
                    return str(o)
                if multilang and o.language:
                    lang_dict[o.language] = str(o)
                elif multilang:
                    lang_dict[DEFAULT_LANG] = str(o)
                else:
                    return str(o)
                if multilang:
                    # when translation does not exist, create an empty one
                    for lang in get_langs():
                        if lang not in lang_dict:
                            lang_dict[lang] = ''
                    return lang_dict
                # Use first object as fallback if no object with the default language is available
                elif fallback == '':
                    fallback = str(o)
            else:
                return str(o)
        return fallback

    def _object_value_multiple_predicate(self, subject, predicates):
        '''
        Given a subject and a list of predicates, returns the value of the object
        according to the order in which it was specified.

        Both subject and predicates must be rdflib URIRef or BNode objects

        If found, the string representation is returned, else an empty string
        '''
        object_value = ''
        for predicate in predicates:
            object_value = self._object_value(subject, predicate)
            if object_value:
                break

        return object_value

    def _object_value_int(self, subject, predicate):
        '''
        Given a subject and a predicate, returns the value of the object as an
        integer

        Both subject and predicate must be rdflib URIRef or BNode objects

        If the value can not be parsed as intger, returns None
        '''
        object_value = self._object_value(subject, predicate)
        if object_value:
            try:
                return int(float(object_value))
            except ValueError:
                pass
        return None

    def _object_value_int_list(self, subject, predicate):
        '''
        Given a subject and a predicate, returns the value of the object as a
        list of integers

        Both subject and predicate must be rdflib URIRef or BNode objects

        If the value can not be parsed as intger, returns an empty list
        '''
        object_values = []
        for object in self.g.objects(subject, predicate):
            if object:
                try:
                    object_values.append(int(float(object)))
                except ValueError:
                    pass
        return object_values

    def _object_value_list(self, subject, predicate):
        '''
        Given a subject and a predicate, returns a list with all the values of
        the objects

        Both subject and predicate must be rdflib URIRef or BNode  objects

        If no values found, returns an empty string
        '''
        return [str(o) for o in self.g.objects(subject, predicate)]

    def _get_vcard_property_value(self, subject, predicate, predicate_string_property=None):
        '''
        Given a subject, a predicate and a predicate for the simple string property (optional),
        returns the value of the object. Trying to read the value in the following order
            * predicate_string_property
            * predicate

        All subject, predicate and predicate_string_property must be rdflib URIRef or BNode  objects

        If no value is found, returns an empty string
        '''

        result = ''
        if predicate_string_property:
            result = self._object_value(subject, predicate_string_property)

        if not result:
            obj = self._object(subject, predicate)
            if isinstance(obj, BNode):
                result = self._object_value(obj, VCARD.hasValue)
            else:
                result = self._object_value(subject, predicate)

        return result

    def _time_interval(self, subject, predicate, dcat_ap_version=1):
        '''
        Returns the start and end date for a time interval object

        Both subject and predicate must be rdflib URIRef or BNode objects

        It checks for time intervals defined with DCAT, W3C Time hasBeginning & hasEnd
        and schema.org startDate & endDate.

        Note that partial dates will be expanded to the first month / day
        value, eg '1904' -> '1904-01-01'.

        Returns a tuple with the start and end date values, both of which
        can be None if not found
        '''

        start_date = end_date = None

        if dcat_ap_version == 1:
            start_date, end_date = self._read_time_interval_schema_org(subject, predicate)
            if start_date or end_date:
                return start_date, end_date
            return self._read_time_interval_time(subject, predicate)
        elif dcat_ap_version == 2:
            start_date, end_date = self._read_time_interval_dcat(subject, predicate)
            if start_date or end_date:
                return start_date, end_date
            start_date, end_date = self._read_time_interval_time(subject, predicate)
            if start_date or end_date:
                return start_date, end_date
            return self._read_time_interval_schema_org(subject, predicate)

    def _read_time_interval_schema_org(self, subject, predicate):
        start_date = end_date = None

        for interval in self.g.objects(subject, predicate):
            start_date = self._object_value(interval, SCHEMA.startDate)
            end_date = self._object_value(interval, SCHEMA.endDate)

            if start_date or end_date:
                return start_date, end_date

        return start_date, end_date

    def _read_time_interval_dcat(self, subject, predicate):
        start_date = end_date = None

        for interval in self.g.objects(subject, predicate):
            start_date = self._object_value(interval, DCAT.startDate)
            end_date = self._object_value(interval, DCAT.endDate)

            if start_date or end_date:
                return start_date, end_date

        return start_date, end_date

    def _read_time_interval_time(self, subject, predicate):
        start_date = end_date = None

        for interval in self.g.objects(subject, predicate):
            start_nodes = [t for t in self.g.objects(interval,
                                                     TIME.hasBeginning)]
            end_nodes = [t for t in self.g.objects(interval,
                                                   TIME.hasEnd)]
            if start_nodes:
                start_date = self._object_value_multiple_predicate(start_nodes[0],
                                            [TIME.inXSDDateTimeStamp, TIME.inXSDDateTime, TIME.inXSDDate])
            if end_nodes:
                end_date = self._object_value_multiple_predicate(end_nodes[0],
                                            [TIME.inXSDDateTimeStamp, TIME.inXSDDateTime, TIME.inXSDDate])

            if start_date or end_date:
                return start_date, end_date

        return start_date, end_date

    def _insert_or_update_temporal(self, dataset_dict, key, value):
        temporal = next((item for item in dataset_dict['extras'] if(item['key'] == key)), None)
        if temporal:
            temporal['value'] = value
        else:
            dataset_dict['extras'].append({'key': key , 'value': value})

    def _publisher(self, subject, predicate):
        '''
        Returns a dict with details about a dct:publisher entity, a foaf:Agent

        Both subject and predicate must be rdflib URIRef or BNode objects

        Examples:

        <dct:publisher>
            <foaf:Organization rdf:about="http://orgs.vocab.org/some-org">
                <foaf:name>Publishing Organization for dataset 1</foaf:name>
                <foaf:mbox>contact@some.org</foaf:mbox>
                <foaf:homepage>http://some.org</foaf:homepage>
                <dct:type rdf:resource="http://purl.org/adms/publishertype/NonProfitOrganisation"/>
                <dct:identifier>EA349s92</dct:identifier>
            </foaf:Organization>
        </dct:publisher>

        {
            'uri': 'http://orgs.vocab.org/some-org',
            'name': 'Publishing Organization for dataset 1',
            'email': 'contact@some.org',
            'url': 'http://some.org',
            'type': 'http://purl.org/adms/publishertype/NonProfitOrganisation',
            'identifier': 'EA349s92'
        }

        <dct:publisher rdf:resource="http://publications.europa.eu/resource/authority/corporate-body/EURCOU" />

        {
            'uri': 'http://publications.europa.eu/resource/authority/corporate-body/EURCOU'
        }

        Returns keys for uri, name, email, url and type with the values set to
        an empty string if they could not be found
        '''

        publisher = {}

        for agent in self.g.objects(subject, predicate):

            publisher['uri'] = (str(agent) if isinstance(agent,
                                rdflib.term.URIRef) else '')

            publisher['name'] = self._object_value(agent, FOAF.name)

            publisher['email'] = self._object_value(agent, FOAF.mbox)

            publisher['url'] = self._object_value(agent, FOAF.homepage)

            publisher['type'] = self._object_value(agent, DCT.type)

            publisher['identifier'] = self._object_value(agent, DCT.identifier)

        return publisher

    def _contact_details(self, subject, predicate):
        '''
        Returns a dict with details about a vcard expression

        Both subject and predicate must be rdflib URIRef or BNode objects

        Examples:

        <dcat:contactPoint>
            <vcard:Organization rdf:nodeID="Nc320885686e84382a0a2ea602ebde399">
                <vcard:fn>Contact Point for dataset 1</vcard:fn>
                <vcard:hasEmail rdf:resource="mailto:contact@some.org"/>
                <vcard:hasURL>http://some.org<vcard:hasURL>
                <vcard:role>pointOfContact<vcard:role>
            </vcard:Organization>
        </dcat:contactPoint>

        {
            'uri': 'http://orgs.vocab.org/some-org',
            'name': 'Contact Point for dataset 1',
            'email': 'contact@some.org',
            'url': 'http://some.org',
            'role': 'pointOfContact',
        }

        Returns keys for uri, name, email and url with the values set to
        an empty string if they could not be found
        '''

        contact = {}

        for agent in self.g.objects(subject, predicate):

            contact['uri'] = (str(agent) if isinstance(agent,
                              rdflib.term.URIRef) else '')

            contact['name'] = self._get_vcard_property_value(agent, VCARD.hasFN, VCARD.fn)

            contact['url'] = self._get_vcard_property_value(agent, VCARD.hasURL)

            contact['role'] = self._get_vcard_property_value(agent, VCARD.role)

            contact['email'] = self._without_mailto(self._get_vcard_property_value(agent, VCARD.hasEmail))

        return contact

    def _parse_geodata(self, spatial, datatype, cur_value):
        '''
        Extract geodata with the given datatype from the spatial data and check if it contains a valid GeoJSON
        or WKT geometry.

        Returns the String or None if the value is no valid GeoJSON or WKT geometry.
        '''
        for geometry in self.g.objects(spatial, datatype):
            if (geometry.datatype == URIRef(GEOJSON_IMT) or
                    not geometry.datatype):
                try:
                    json.loads(str(geometry))
                    cur_value = str(geometry)
                except (ValueError, TypeError):
                    pass
            if not cur_value and geometry.datatype == GSP.wktLiteral:
                try:
                    cur_value = json.dumps(wkt.loads(str(geometry)))
                except (ValueError, TypeError):
                    pass
        return cur_value


    def _search_values_codelist_add_to_graph(self, metadata_codelist, labels, dataset_dict, dataset_ref, dataset_tag_base, g, dcat_property):
        # Crear un diccionario con label como clave e id como valor para cada elemento en metadata_codelist
        inspire_dict = {row['label'].lower(): row.get('id', row.get('value')) for row in metadata_codelist}
        
        # Verificar si labels es una lista, si no, convertirlo en una lista
        if not isinstance(labels, list):
            labels = [labels]
        
        # Obtener el valor de 'topic' del dataset_dict
        topic_value = self._get_dataset_value(dataset_dict, 'topic')
        
        # Si topic_value es None, asignar una lista vacía para evitar errores
        if topic_value is None:
            topic_value = []
        
        for label in labels:
            if label not in topic_value:
                # Comprobar si tag_name está en inspire_dict
                if label.lower() in inspire_dict:
                    tag_val = inspire_dict[label.lower()]
                else:
                    tag_val = f'{dataset_tag_base}/dataset/?tags={label}'
                g.add((dataset_ref, dcat_property, URIRefOrLiteral(tag_val)))

    def dict_to_list(value):
        """Converts a dictionary to a list of its values.

        Args:
            value: The value to convert.

        Returns:
            If the value is a dictionary, returns a list of its values. Otherwise, returns the value unchanged.
        """
        if isinstance(value, dict):
            value = list(value.values())
        return value

    def _get_localized_dataset_value(multilang_dict, default=None):
        """Returns a localized dataset multilang_dict.

        Args:
            multilang_dict: A string or dictionary representing the multilang_dict to localize.
            default: A default value to return if the multilang_dict cannot be localized.

        Returns:
            A dictionary representing the localized multilang_dict, or the default value if the multilang_dict cannot be localized.

        Raises:
            None.
        """
        if isinstance(multilang_dict, dict):
            return multilang_dict

        if isinstance(multilang_dict, str):
            try:
                multilang_dict = json.loads(multilang_dict)
            except ValueError:
                return default

    def _search_value_codelist(self, metadata_codelist, label, input_field_name, output_field_name, return_value=True):
        """Searches for a value in a metadata codelist.

        Args:
            metadata_codelist (list): A list of dictionaries containing the metadata codelist.
            label (str): The label to search for in the codelist.
            input_field_name (str): The name of the input field in the codelist.
            output_field_name (str): The name of the output field in the codelist.
            return_value (bool): Whether to return the label value if not found (True) or None if not found (False). Default is True.

        Returns:
            str or None: The value found in the codelist, or None if not found and return_value is False.
        """
        inspire_dict = {row[input_field_name].lower(): row[output_field_name] for row in metadata_codelist}
        tag_val = inspire_dict.get(label.lower(), None)
        if not return_value and tag_val is None:
            return None
        elif not return_value and tag_val:
            return tag_val        
        elif return_value == True and tag_val is None:
            return label
        else:
            return tag_val

    def _spatial(self, subject, predicate):
        '''
        Returns a dict with details about the spatial location

        Both subject and predicate must be rdflib URIRef or BNode objects

        Returns keys for uri, text or geom with the values set to
        None if they could not be found.

        Geometries are always returned in WKT. If only GeoJSON is provided,
        it will be transformed to WKT.

        Check the notes on the README for the supported formats:

        https://github.com/ckan/ckanext-dcat/#rdf-dcat-to-ckan-dataset-mapping
        '''

        uri = None
        text = None
        geom = None
        bbox = None
        cent = None

        for spatial in self.g.objects(subject, predicate):

            if isinstance(spatial, URIRef):
                uri = str(spatial)

            if isinstance(spatial, Literal):
                text = str(spatial)

            if (spatial, RDF.type, DCT.Location) in self.g:
                geom = self._parse_geodata(spatial, LOCN.geometry, geom)
                bbox = self._parse_geodata(spatial, DCAT.bbox, bbox)
                cent = self._parse_geodata(spatial, DCAT.centroid, cent)
                for label in self.g.objects(spatial, SKOS.prefLabel):
                    text = str(label)
                for label in self.g.objects(spatial, RDFS.label):
                    text = str(label)

        return {
            'uri': uri,
            'text': text,
            'geom': geom,
            'bbox': bbox,
            'centroid': cent,
        }

    def _license(self, dataset_ref):
        '''
        Returns a license identifier if one of the distributions license is
        found in CKAN license registry. If no distribution's license matches,
        an empty string is returned.

        The first distribution with a license found in the registry is used so
        that if distributions have different licenses we'll only get the first
        one.
        '''
        if self._licenceregister_cache is not None:
            license_uri2id, license_title2id = self._licenceregister_cache
        else:
            license_uri2id = {}
            license_title2id = {}
            for license_id, license in list(LicenseRegister().items()):
                license_uri2id[license.url] = license_id
                license_title2id[license.title] = license_id
            self._licenceregister_cache = license_uri2id, license_title2id

        for distribution in self._distributions(dataset_ref):
            # If distribution has a license, attach it to the dataset
            license = self._object(distribution, DCT.license)
            if license:
                # Try to find a matching license comparing URIs, then titles
                license_id = license_uri2id.get(license.toPython())
                if not license_id:
                    license_id = license_title2id.get(
                        self._object_value(license, DCT.title))
                if license_id:
                    return license_id
        return ''

    def _access_rights(self, subject, predicate):
        '''
        Returns the rights statement or an empty string if no one is found.
        '''

        result = ''
        obj = self._object(subject, predicate)
        if obj:
            if isinstance(obj, BNode) and self._object(obj, RDF.type) == DCT.RightsStatement:
                result = self._object_value(obj, RDFS.label)
            elif isinstance(obj, Literal) or isinstance(obj, URIRef):
                # unicode_safe not include Literal or URIRef
                result = str(obj)
        return result

    def _distribution_format(self, distribution, normalize_ckan_format=True):
        '''
        Returns the Internet Media Type and format label for a distribution

        Given a reference (URIRef or BNode) to a dcat:Distribution, it will
        try to extract the media type (previously knowm as MIME type), eg
        `text/csv`, and the format label, eg `CSV`

        Values for the media type will be checked in the following order:

        1. literal value of dcat:mediaType
        2. literal value of dct:format if it contains a '/' character
        3. value of dct:format if it is an instance of dct:IMT, eg:

            <dct:format>
                <dct:IMT rdf:value="text/html" rdfs:label="HTML"/>
            </dct:format>
        4. value of dct:format if it is an URIRef and appears to be an IANA type

        Values for the label will be checked in the following order:

        1. literal value of dct:format if it not contains a '/' character
        2. label of dct:format if it is an instance of dct:IMT (see above)
        3. value of dct:format if it is an URIRef and doesn't look like an IANA type

        If `normalize_ckan_format` is True the label will
        be tried to match against the standard list of formats that is included
        with CKAN core
        (https://github.com/ckan/ckan/blob/master/ckan/config/resource_formats.json)
        This allows for instance to populate the CKAN resource format field
        with a format that view plugins, etc will understand (`csv`, `xml`,
        etc.)

        Return a tuple with the media type and the label, both set to None if
        they couldn't be found.
        '''

        imt = None
        label = None

        imt = self._object_value(distribution, DCAT.mediaType)

        _format = self._object(distribution, DCT['format'])
        if isinstance(_format, Literal):
            if not imt and '/' in _format:
                imt = str(_format)
            else:
                label = str(_format)
        elif isinstance(_format, (BNode, URIRef)):
            if self._object(_format, RDF.type) == DCT.IMT:
                if not imt:
                    imt = str(self.g.value(_format, default=None))
                label = str(self.g.label(_format, default=None))
            elif isinstance(_format, URIRef):
                # If the URIRef does not reference a BNode, it could reference an IANA type.
                # Otherwise, use it as label.
                format_uri = str(_format)
                if 'iana.org/assignments/media-types' in format_uri and not imt:
                    imt = format_uri
                else:
                    label = format_uri

        if ((imt or label) and normalize_ckan_format):
            import ckan.config
            from ckan.lib import helpers

            format_registry = helpers.resource_formats()

            if imt in format_registry:
                label = format_registry[imt][1]
            elif label in format_registry:
                label = format_registry[label][1]

        return imt, label

    def _get_dict_value(self, _dict, key, default=None):
        '''
        Returns the value for the given key on a CKAN dict

        By default a key on the root level is checked. If not found, extras
        are checked, both with the key provided and with `dcat_` prepended to
        support legacy fields.

        If not found, returns the default value, which defaults to None
        '''

        if key in _dict:
            return _dict[key]

        for extra in _dict.get('extras', []):
            if extra['key'] == key or extra['key'] == 'dcat_' + key:
                return extra['value']

        return default

    def _read_list_value(self, value):
        items = []
        # List of values
        if isinstance(value, list):
            items = value
        elif isinstance(value, basestring):
            try:
                items = json.loads(value)
                if isinstance(items, ((int, float, complex))):
                    items = [items]  # JSON list
            except ValueError:
                if ',' in value:
                    # Comma-separated list
                    items = value.split(',')
                else:
                    items = [value]  # Normal text value
        return items

    def _add_geojson_value_to_graph(self, spatial_ref, predicate, value):
        '''
        Adds GeoJSON spatial triples to the graph.
        '''
        self.g.add((spatial_ref,
                    predicate,
                    Literal(value, datatype=GEOJSON_IMT)))

    def _add_wkt_or_geojson_value_to_graph(self, spatial_ref, predicate, value):
        """
        Adds a WKT or GeoJSON value to an RDF graph.

        Args:
            spatial_ref: The RDF subject reference.
            predicate: The RDF predicate for the spatial data.
            value: The GeoJSON or GeoJSON string to be added.

        Note:
            This function will first attempt to add the value as a WKT and, if unsuccessful, add it as a GeoJSON literal, to comply with GeoDCAT-AP, which states that the geometries MAY be provided in multiple encodings, but at least one of the following MUST be provided GML and WKT.(https://semiceu.github.io/GeoDCAT-AP/drafts/latest/#bounding-box)

        Raises:
            TypeError: If the input value is of an unsupported type.
            ValueError: If there's an error in converting GeoJSON to WKT.
            InvalidGeoJSONException: If the GeoJSON is invalid.
        """
        try:
            if isinstance(value, str):
                # Try to parse the input as a GeoJSON string
                geojson_obj = json.loads(value)
            else:
                geojson_obj = value

            # Attempt to convert the GeoJSON object to WKT with 4 decimals
            wkt_string = wkt.dumps(geojson_obj, decimals=4)

            # Create a Literal with the WKT representation and add it to the RDF graph
            wkt_literal = Literal(wkt_string, datatype=GSP.wktLiteral)
            self.g.add((spatial_ref, predicate, wkt_literal))
            
        except (TypeError, ValueError, InvalidGeoJSONException):
            # If WKT conversion fails, add the value as a GeoJSON literal
            geojson_literal = Literal(value, datatype=GSP.geoJSONLiteral)
            self.g.add((spatial_ref, predicate, geojson_literal))

    def _add_wkt_value_to_graph(self, spatial_ref, predicate, value):
        '''
        Adds WKT spatial triples to the graph.
        '''
        try:
            self.g.add((spatial_ref,
                        predicate,
                        Literal(wkt.dumps(json.loads(value),
                                            decimals=4),
                                datatype=GSP.wktLiteral)))
        except (TypeError, ValueError, InvalidGeoJSONException):
            pass

    def _add_spatial_to_dict(self, dataset_dict, key, spatial):
        if spatial.get(key):
            dataset_dict['extras'].append(
                {'key': 'spatial_{0}'.format(key) if key != 'geom' else 'spatial',
                 'value': spatial.get(key)})

    def _get_dataset_value(self, dataset_dict, key, default=None):
        '''
        Returns the value for the given key on a CKAN dict

        Check `_get_dict_value` for details
        '''
        return self._get_dict_value(dataset_dict, key, default)

    def _get_resource_value(self, resource_dict, key, default=None):
        '''
        Returns the value for the given key on a CKAN dict

        Check `_get_dict_value` for details
        '''
        return self._get_dict_value(resource_dict, key, default)

    def _add_date_triples_from_dict(self, _dict, subject, items):
        self._add_triples_from_dict(_dict, subject, items,
                                    date_value=True)

    def _add_list_triples_from_dict(self, _dict, subject, items):
        self._add_triples_from_dict(_dict, subject, items,
                                    list_value=True)

    def _add_triples_from_dict(self, _dict, subject, items,
                               list_value=False,
                               date_value=False,
                               multilang=False):
        for item in items:
            if len(item) == 5:
                key, predicate, fallbacks, _type, required_lang = item
            else:
                key, predicate, fallbacks, _type = item
                required_lang = None

            predicate = URIRef(predicate)

            self._add_triple_from_dict(_dict, subject, predicate, key,
                                    fallbacks=fallbacks,
                                    list_value=list_value,
                                    date_value=date_value,
                                    multilang=multilang,
                                    _type=_type,
                                    required_lang=required_lang)

    def _add_triple_from_dict(self, _dict, subject, predicate, key,
                              fallbacks=None,
                              list_value=False,
                              date_value=False,
                              multilang=False,
                              _type=Literal,
                              _datatype=None,
                              value_modifier=None,
                              required_lang= None):
        '''
        Adds a new triple to the graph with the provided parameters

        The subject and predicate of the triple are passed as the relevant
        RDFLib objects (URIRef or BNode). As default, the object is a
        literal value, which is extracted from the dict using the provided key
        (see `_get_dict_value`). If the value for the key is not found, then
        additional fallback keys are checked.
        Using `value_modifier`, a function taking the extracted value and
        returning a modified value can be passed.
        If a value was found, the modifier is applied before adding the value.

        If `list_value` or `date_value` are True, then the value is treated as
        a list or a date respectively (see `_add_list_triple` and
        `_add_date_triple` for details.
        '''
        value = self._get_dict_value(_dict, key)
        if not value and fallbacks:
            for fallback in fallbacks:
                value = self._get_dict_value(_dict, fallback)
                if value:
                    break

        # if a modifying function was given, apply it to the value
        if value and callable(value_modifier):
            value = value_modifier(value)

        if value and list_value:
            self._add_list_triple(subject, predicate, value, _type, _datatype)
        elif value and date_value:
            self._add_date_triple(subject, predicate, value, _type)
        # if multilang is True, the value is a dict with language codes as keys
        elif value and multilang:
            self._add_multilang_triple(subject, predicate, value, required_lang)
        elif value:
            # Normal text value
            # ensure URIRef items are preprocessed (space removal/url encoding)
            if _type == URIRef:
                _type = CleanedURIRef
            if _datatype:
                object = _type(value, datatype=_datatype)
            else:
                object = _type(value)
            self.g.add((subject, predicate, object))

    def _add_multilang_triple(self, subject, predicate, multilang_values, required_lang=None):
        #log.debug('multilang_values: {0}'.format(multilang_values))

        if not multilang_values:
            return

        if isinstance(multilang_values, str):
            try:
                multilang_values = json.loads(multilang_values)
            except ValueError:
                pass

        if isinstance(multilang_values, dict):
            try:
                for key, value in multilang_values.items():
                    if value and (not required_lang or key == required_lang):
                        self.g.add((subject, predicate, Literal(value, lang=key)))
            except (ValueError, KeyError):
                self.g.add((subject, predicate, Literal(multilang_values)))

        else:
            self.g.add((subject, predicate, Literal(multilang_values)))

    def _add_list_triple(self, subject, predicate, value, _type=Literal, _datatype=None):
        '''
        Adds as many triples to the graph as values

        Values are literal strings, if `value` is a list, one for each
        item. If `value` is a string there is an attempt to split it using
        commas, to support legacy fields.
        '''
        items = self._read_list_value(value)

        for item in items:
            # ensure URIRef items are preprocessed (space removal/url encoding)
            if _type == URIRef:
                _type = CleanedURIRef
            if _datatype:
                object = _type(item, datatype=_datatype)
            else:
                object = _type(item)
            self.g.add((subject, predicate, object))

    def _add_date_triple(self, subject, predicate, value, _type=Literal):
        '''
        Adds a new triple with a date object

        Dates are parsed using dateutil, and if the date obtained is correct,
        added to the graph as an XSD.dateTime value.

        If there are parsing errors, the literal string value is added.
        '''
        if not value:
            return
        try:
            default_datetime = datetime.datetime(1, 1, 1, 0, 0, 0)
            _date = parse_date(value, default=default_datetime)

            self.g.add((subject, predicate, _type(_date.isoformat(),
                                                  datatype=XSD.dateTime)))
        except ValueError:
            self.g.add((subject, predicate, _type(value)))

    def _get_catalog_field(self, field_name, default_values_dict=euro_dcat_ap_default_values, fallback=None, return_none=False, order='desc'):
        '''
        Returns the value of a field from the most recently modified dataset.

        Args:
            field_name (str): The name of the field to retrieve from datasets list
            default_values_dict (dict): A dictionary of default values to use if the field is not found. Defaults to euro_dcat_ap_default_values.
            fallback (str): The name of the fallback field to retrieve if `field_name` is not found. Defaults to None.
            return_none (bool): Whether to return None if the field is not found. Defaults to False.
            order (str): The order in which to sort the results. Defaults to 'desc'.

        Returns:
            The value of the field, or the default value if it could not be found and `return_none` is False.

        Notes:
            This function caches the result of the CKAN API call to improve performance. The cache is stored in the `_catalog_search_result` attribute of the object. If the attribute exists, the function will return the value of the requested field from the cached result instead of making a new API call. If the attribute does not exist, the function will make a new API call and store the result in the attribute for future use.
        '''
        if not hasattr(self, '_catalog_search_result'):
            context = {
                'ignore_auth': True
            }
            self._catalog_search_result = toolkit.get_action('package_search')(context, {
                'sort': f'metadata_modified {order}',
                'rows': 1,
            })
        try:
            if self._catalog_search_result and self._catalog_search_result.get('results'):
                return self._catalog_search_result['results'][0][field_name]
        except KeyError:
            pass
        if fallback:
            try:
                if self._catalog_search_result and self._catalog_search_result.get('results'):
                    return self._catalog_search_result['results'][0][fallback]
            except KeyError:
                pass
        if return_none:
            return None
        return default_values_dict[field_name]

    def _get_catalog_dcat_theme(self):
        '''
        Returns the value of the `theme_es` field from the most recently created dataset,
        or the value of the `theme_eu` field if `theme_es` is not present.

        Returns None if neither `theme_es` nor `theme_eu` are present in the dataset.

        Returns:
            A string with the value of the `theme_es` or `theme_eu` field, or None if not found.
        '''
        context = {
            'ignore_auth': True
        }
        result = toolkit.get_action('package_search')(context, {
            'sort': 'metadata_created desc',
            'rows': 1,
        })
        if result and result.get('results'):
            if 'theme_es' in result['results'][0]:
                return result['results'][0]['theme_es'][0]
            elif 'theme_eu' in result['results'][0]:
                return result['results'][0]['theme_eu'][0]
        return None

    def _get_catalog_language(self, default_values=euro_dcat_ap_default_values):
        '''
        Returns the value of the `language` field from the most recently modified dataset.

        Args:
            default_values (dict): A dictionary of default values to use if the `publisher_uri` field cannot be found.

        Returns:
            A string with the value of the `language` field, or None if it could not be found.
        '''
        context = {
            'ignore_auth': True
        }
        result = toolkit.get_action('package_search')(context, {
            'sort': 'metadata_modified desc',
            'rows': 1,
        })
        try:
            if result and result.get('results'):
                return result['results'][0]['language']
            
        except KeyError:
            return default_values['language']

    def _last_catalog_modification(self):
        '''
        Returns the date and time the catalog was last modified

        To be more precise, the most recent value for `metadata_modified` on a
        dataset.

        Returns a dateTime string in ISO format, or None if it could not be
        found.
        '''
        context = {
            'ignore_auth': True
        }
        result = toolkit.get_action('package_search')(context, {
            'sort': 'metadata_modified desc',
            'rows': 1,
        })
        if result and result.get('results'):
            return result['results'][0]['metadata_modified']
        return None

    def _add_mailto(self, mail_addr):
        '''
        Ensures that the mail address has an URIRef-compatible mailto: prefix.
        Can be used as modifier function for `_add_triple_from_dict`.
        '''
        if mail_addr:
            return PREFIX_MAILTO + self._without_mailto(mail_addr)
        else:
            return mail_addr

    def _without_mailto(self, mail_addr):
        '''
        Ensures that the mail address string has no mailto: prefix.
        '''
        if mail_addr:
            return str(mail_addr).replace(PREFIX_MAILTO, u'')
        else:
            return mail_addr

    def _get_source_catalog(self, dataset_ref):
        '''
        Returns Catalog reference that is source for this dataset.

        Catalog referenced in dct:hasPart is returned,
        if dataset is linked there, otherwise main catalog
        will be returned.

        This will not be used if ckanext.dcat.expose_subcatalogs
        configuration option is set to False.
        '''
        if not toolkit.asbool(config.get(DCAT_EXPOSE_SUBCATALOGS, False)):
            return
        catalogs = set(self.g.subjects(DCAT.dataset, dataset_ref))
        root = self._get_root_catalog_ref()
        try:
            catalogs.remove(root)
        except KeyError:
            pass
        assert len(catalogs) in (0, 1,), "len %s" %catalogs
        if catalogs:
            return catalogs.pop()
        return root

    def _get_root_catalog_ref(self):
        roots = list(self.g.subjects(DCT.hasPart))
        if not roots:
            roots = list(self.g.subjects(RDF.type, DCAT.Catalog))
        return roots[0]

    def _get_or_create_spatial_ref(self, dataset_dict, dataset_ref):
        for spatial_ref in self.g.objects(dataset_ref, DCT.spatial):
            if spatial_ref:
                return spatial_ref

        # Create new spatial_ref
        spatial_uri = self._get_dataset_value(dataset_dict, 'spatial_uri')
        if spatial_uri:
            spatial_ref = CleanedURIRef(spatial_uri)
        else:
            spatial_ref = BNode()
        self.g.add((spatial_ref, RDF.type, DCT.Location))
        self.g.add((dataset_ref, DCT.spatial, spatial_ref))
        return spatial_ref

    # Public methods for profiles to implement

    def parse_dataset(self, dataset_dict, dataset_ref):
        '''
        Creates a CKAN dataset dict from the RDF graph

        The `dataset_dict` is passed to all the loaded profiles before being
        yielded, so it can be further modified by each one of them.
        `dataset_ref` is an rdflib URIRef object
        that can be used to reference the dataset when querying the graph.

        Returns a dataset dict that can be passed to eg `package_create`
        or `package_update`
        '''
        return dataset_dict

    def _extract_catalog_dict(self, catalog_ref):
        '''
        Returns list of key/value dictionaries with catalog
        '''

        out = []
        sources = (('source_catalog_title', DCT.title,),
                   ('source_catalog_description', DCT.description,),
                   ('source_catalog_homepage', FOAF.homepage,),
                   ('source_catalog_language', DCT.language,),
                   ('source_catalog_modified', DCT.modified,),)

        for key, predicate in sources:
            val = self._object_value(catalog_ref, predicate)
            if val:
                out.append({'key': key, 'value': val})

        out.append({'key': 'source_catalog_publisher', 'value': json.dumps(self._publisher(catalog_ref, DCT.publisher))})
        return out

    def graph_from_catalog(self, catalog_dict, catalog_ref):
        '''
        Creates an RDF graph for the whole catalog (site)

        The class RDFLib graph (accessible via `self.g`) should be updated on
        this method

        `catalog_dict` is a dict that can contain literal values for the
        dcat:Catalog class like `title`, `homepage`, etc. `catalog_ref` is an
        rdflib URIRef object that must be used to reference the catalog when
        working with the graph.
        '''
        pass

    def graph_from_dataset(self, dataset_dict, dataset_ref):
        '''
        Given a CKAN dataset dict, creates an RDF graph

        The class RDFLib graph (accessible via `self.g`) should be updated on
        this method

        `dataset_dict` is a dict with the dataset metadata like the one
        returned by `package_show`. `dataset_ref` is an rdflib URIRef object
        that must be used to reference the dataset when working with the graph.
        '''
        pass


class EuropeanDCATAPProfile(RDFProfile):
    '''
    An RDF profile based on the DCAT-AP for data portals in Europe

    Default values for some fields:
    
    ckanext-dcat/ckanext/dcat/profiles_default.py

    More information and specification:

    https://joinup.ec.europa.eu/asset/dcat_application_profile

    '''

    def parse_dataset(self, dataset_dict, dataset_ref):

        dataset_dict['extras'] = []
        dataset_dict['resources'] = []

        # Translated fields
        for key, field in default_translated_fields.items():
            predicate = field['rdf_predicate']
            value = self._object_value(dataset_ref, predicate, True)
            if value:
                dataset_dict[field['field_name']] = value

        # Basic fields
        for key, predicate in (
                ('title', DCT.title),
                ('notes', DCT.description),
                ('url', DCAT.landingPage),
                ('version', OWL.versionInfo),
                ('encoding', CNT.characterEncoding)
                ):
            value = self._object_value(dataset_ref, predicate)
            if value:
                dataset_dict[key] = value

        if not dataset_dict.get('version'):
            # adms:version was supported on the first version of the DCAT-AP
            value = self._object_value(dataset_ref, ADMS.version)
            if value:
                dataset_dict['version'] = value

        # Tags
        # replace munge_tag to noop if there's no need to clean tags
        do_clean = toolkit.asbool(config.get(DCAT_CLEAN_TAGS, False))
        tags_val = [munge_tag(tag) if do_clean else tag for tag in self._keywords(dataset_ref)]
        tags = [{'name': tag} for tag in tags_val]
        dataset_dict['tags'] = tags

        # Extras

        #  Simple values
        for key, predicate in (
                ('issued', DCT.issued),
                ('modified', DCT.modified),
                ('identifier', DCT.identifier),
                ('version_notes', ADMS.versionNotes),
                ('frequency', DCT.accrualPeriodicity),
                ('provenance', DCT.provenance),
                ('dcat_type', DCT.type),
                ):
            value = self._object_value(dataset_ref, predicate)
            if value:
                dataset_dict['extras'].append({'key': key, 'value': value})

        #  Lists
        for key, predicate, in (
                ('language', DCT.language),
                ('theme', DCAT.theme),
                (metadata_field_names['spain_dcat']['theme'], DCAT.theme),
                ('alternate_identifer', ADMS.identifier),
                ('inspire_id', ADMS.identifier),
                ('conforms_to', DCT.conformsTo),
                ('metadata_profile', DCT.conformsTo),
                ('documentation', FOAF.page),
                ('related_resource', DCT.relation),
                ('has_version', DCT.hasVersion),
                ('is_version_of', DCT.isVersionOf),
                ('lineage_source', DCT.source),
                ('source', DCT.source),
                ('sample', ADMS.sample),
                ):
            values = self._object_value_list(dataset_ref, predicate)
            if values:
                dataset_dict['extras'].append({'key': key,
                                               'value': json.dumps(values)})

        # Contact details
        contact = self._contact_details(dataset_ref, DCAT.contactPoint)
        if not contact:
            # adms:contactPoint was supported on the first version of DCAT-AP
            contact = self._contact_details(dataset_ref, ADMS.contactPoint)

        if contact:
            for key in ('uri', 'name', 'email', 'url', 'role'):
                if contact.get(key):
                    dataset_dict['extras'].append(
                        {'key': 'contact_{0}'.format(key),
                         'value': contact.get(key)})

        # Publisher
        publisher = self._publisher(dataset_ref, DCT.publisher)
        for key in ('uri', 'name', 'email', 'url', 'type', 'identifier'):
            if publisher.get(key):
                dataset_dict['extras'].append(
                    {'key': 'publisher_{0}'.format(key),
                     'value': publisher.get(key)})

        # Temporal
        start, end = self._time_interval(dataset_ref, DCT.temporal)
        if start:
            dataset_dict['extras'].append(
                {'key': 'temporal_start', 'value': start})
        if end:
            dataset_dict['extras'].append(
                {'key': 'temporal_end', 'value': end})

        # Spatial
        spatial = self._spatial(dataset_ref, DCT.spatial)
        for key in ('uri', 'text', 'geom'):
            self._add_spatial_to_dict(dataset_dict, key, spatial)

        # Dataset URI (explicitly show the missing ones)
        dataset_uri = (str(dataset_ref)
                       if isinstance(dataset_ref, rdflib.term.URIRef)
                       else '')
        dataset_dict['extras'].append({'key': 'uri', 'value': dataset_uri})

        # License
        if 'license_id' not in dataset_dict:
            dataset_dict['license_id'] = self._license(dataset_ref)

        # Source Catalog
        if toolkit.asbool(config.get(DCAT_EXPOSE_SUBCATALOGS, False)):
            catalog_src = self._get_source_catalog(dataset_ref)
            if catalog_src is not None:
                src_data = self._extract_catalog_dict(catalog_src)
                dataset_dict['extras'].extend(src_data)

        # Resources
        for distribution in self._distributions(dataset_ref):

            resource_dict = {}

            #  Simple values
            for key, predicate in (
                    ('name', DCT.title),
                    ('description', DCT.description),
                    ('access_url', DCAT.accessURL),
                    ('download_url', DCAT.downloadURL),
                    ('issued', DCT.issued),
                    ('modified', DCT.modified),
                    ('status', ADMS.status),
                    ('license', DCT.license),
                    ):
                value = self._object_value(distribution, predicate)
                if value:
                    resource_dict[key] = value

            resource_dict['url'] = (self._object_value(distribution,
                                                       DCAT.downloadURL) or
                                    self._object_value(distribution,
                                                       DCAT.accessURL))
            #  Lists
            for key, predicate in (
                    ('language', DCT.language),
                    ('documentation', FOAF.page),
                    ('conforms_to', DCT.conformsTo),
                    ('metadata_profile', DCT.conformsTo),
                    ):
                values = self._object_value_list(distribution, predicate)
                if values:
                    resource_dict[key] = json.dumps(values)

            # rights
            rights = self._access_rights(distribution, DCT.rights)
            if rights:
                resource_dict['rights'] = rights

            # Format and media type
            normalize_ckan_format = toolkit.asbool(config.get(
                'ckanext.dcat.normalize_ckan_format', True))
            imt, label = self._distribution_format(distribution,
                                                   normalize_ckan_format)

            if imt:
                resource_dict['mimetype'] = imt

            if label:
                resource_dict['format'] = label
            elif imt:
                resource_dict['format'] = imt

            # Size
            size = self._object_value_int(distribution, DCAT.byteSize)
            if size is not None:
                resource_dict['size'] = size

            # Checksum
            for checksum in self.g.objects(distribution, SPDX.checksum):
                algorithm = self._object_value(checksum, SPDX.algorithm)
                checksum_value = self._object_value(checksum, SPDX.checksumValue)
                if algorithm:
                    resource_dict['hash_algorithm'] = algorithm
                if checksum_value:
                    resource_dict['hash'] = checksum_value

            # Distribution URI (explicitly show the missing ones)
            resource_dict['uri'] = (str(distribution)
                                    if isinstance(distribution,
                                                  rdflib.term.URIRef)
                                    else '')

            # Remember the (internal) distribution reference for referencing in
            # further profiles, e.g. for adding more properties
            resource_dict['distribution_ref'] = str(distribution)

            dataset_dict['resources'].append(resource_dict)

        if self.compatibility_mode:
            # Tweak the resulting dict to make it compatible with previous
            # versions of the ckanext-dcat parsers
            for extra in dataset_dict['extras']:
                if extra['key'] in ('issued', 'modified', 'publisher_name',
                                    'publisher_email',):

                    extra['key'] = 'dcat_' + extra['key']

                if extra['key'] == 'language':
                    extra['value'] = ','.join(
                        sorted(json.loads(extra['value'])))

        return dataset_dict

    def graph_from_dataset(self, dataset_dict, dataset_ref):

        g = self.g

        for prefix, namespace in namespaces.items():
            g.bind(prefix, namespace)

        g.add((dataset_ref, RDF.type, DCAT.Dataset))

        # Translated fields
        items = [(
            default_translated_fields[key]['field_name'],
            default_translated_fields[key]['rdf_predicate'],
            default_translated_fields[key]['fallbacks'],
            default_translated_fields[key]['_type']
            )
            for key in default_translated_fields
        ]
        self._add_triples_from_dict(dataset_dict, dataset_ref, items, multilang=True)

        # Basic fields
        items = [
            ('encoding', CNT.characterEncoding, None, Literal),
            ('url', DCAT.landingPage, None, URIRef),
            ('identifier', DCT.identifier, ['guid', 'id'], URIRefOrLiteral),
            ('version', OWL.versionInfo, ['dcat_version'], Literal),
            ('frequency', DCT.accrualPeriodicity, None, URIRefOrLiteral),
            ('dcat_type', DCT.type, None, URIRefOrLiteral),
            ('topic', DCAT.keyword, None, URIRefOrLiteral),
        ]
        self._add_triples_from_dict(dataset_dict, dataset_ref, items)

        # Access Rights
        # DCAT-AP: http://publications.europa.eu/en/web/eu-vocabularies/at-dataset/-/resource/dataset/access-right
        if self._get_dataset_value(dataset_dict, 'access_rights') and 'authority/access-right' in self._get_dataset_value(dataset_dict, 'access_rights'):
                g.add((dataset_ref, DCT.accessRights, URIRef(self._get_dataset_value(dataset_dict, 'access_rights'))))
        else:
            g.add((dataset_ref, DCT.accessRights, URIRef(euro_dcat_ap_default_values['access_rights'])))
            
        # Tags
        # Pre-process keywords inside INSPIRE MD Codelists and update dataset_dict
        dataset_tag_base = f"{dataset_ref.split('/dataset/')[0]}"
        tag_names = [tag['name'].replace(" ", "").lower() for tag in dataset_dict.get('tags', [])]

        # Search for matching keywords in MD_INSPIRE_REGISTER and update dataset_dict
        if tag_names:             
            self._search_values_codelist_add_to_graph(MD_INSPIRE_REGISTER, tag_names, dataset_dict, dataset_ref, dataset_tag_base, g, DCAT.keyword)

        # Dates
        items = [
            ('created', DCT.created, ['metadata_created'], Literal),
            ('issued', DCT.issued, ['metadata_created'], Literal),
            ('modified', DCT.modified, ['metadata_modified'], Literal),
        ]
        self._add_date_triples_from_dict(dataset_dict, dataset_ref, items)

        #  Lists
        items = [
            ('language', DCT.language, None, URIRefOrLiteral),
            ('theme', DCAT.theme, None, URIRef),
            (metadata_field_names['spain_dcat']['theme'], DCAT.theme, None, URIRef),
            ('conforms_to', DCT.conformsTo, None, URIRef),
            ('metadata_profile', DCT.conformsTo, None, URIRef),
            ('alternate_identifier', ADMS.identifier, None, URIRefOrLiteral),
            ('inspire_id', ADMS.identifier, None, URIRefOrLiteral),
            ('documentation', FOAF.page, None, URIRefOrLiteral),
            ('related_resource', DCT.relation, None, URIRefOrLiteral),
            ('has_version', DCT.hasVersion, None, URIRefOrLiteral),
            ('is_version_of', DCT.isVersionOf, None, URIRefOrLiteral),
            ('lineage_source', DCT.source, None, Literal),
            ('source', DCT.source, None, URIRefOrLiteral),
            ('sample', ADMS.sample, None, URIRefOrLiteral),
        ]
        self._add_list_triples_from_dict(dataset_dict, dataset_ref, items)

        # DCAT Themes (https://publications.europa.eu/resource/authority/data-theme)
        # Append the final result to the graph
        dcat_themes = self._themes(dataset_ref)
        for theme in dcat_themes:
            g.add((dataset_ref, DCAT.theme, URIRefOrLiteral(theme)))

        # Contact details
        if any([
            self._get_dataset_value(dataset_dict, 'contact_uri'),
            self._get_dataset_value(dataset_dict, 'contact_name'),
            self._get_dataset_value(dataset_dict, 'contact_email'),
            self._get_dataset_value(dataset_dict, 'contact_url'),
        ]):

            contact_uri = self._get_dataset_value(dataset_dict, 'contact_uri')
            if contact_uri:
                contact_details = CleanedURIRef(contact_uri)
            else:
                contact_details = BNode()

            g.add((contact_details, RDF.type, VCARD.Organization))
            g.add((dataset_ref, DCAT.contactPoint, contact_details))

            # Add name
            self._add_triple_from_dict(
                dataset_dict, contact_details,
                VCARD.fn, 'contact_name'
            )
            # Add mail address as URIRef, and ensure it has a mailto: prefix
            self._add_triple_from_dict(
                dataset_dict, contact_details,
                VCARD.hasEmail, 'contact_email',
                _type=URIRef, value_modifier=self._add_mailto
            )
            # Add contact URL
            self._add_triple_from_dict(
                dataset_dict, contact_details,
                VCARD.hasURL, 'contact_url',
                _type=URIRef)
            
            # Add contact role
            g.add((contact_details, VCARD.role, URIRef(euro_dcat_ap_default_values['contact_role'])))

        # Resource maintainer/contact 
        if any([
            self._get_dataset_value(dataset_dict, 'maintainer'),
            self._get_dataset_value(dataset_dict, 'maintainer_uri'),
            self._get_dataset_value(dataset_dict, 'maintainer_email'),
            self._get_dataset_value(dataset_dict, 'maintainer_url'),
        ]):
            maintainer_uri = self._get_dataset_value(dataset_dict, 'maintainer_uri')
            if maintainer_uri:
                maintainer_details = CleanedURIRef(maintainer_uri)
            else:
                maintainer_details = dataset_ref + '/maintainer'
                
            g.add((maintainer_details, RDF.type, VCARD.Individual))
            g.add((dataset_ref, DCAT.contactPoint, maintainer_details))

            ## Add name & mail
            self._add_triple_from_dict(
                dataset_dict, maintainer_details,
                VCARD.fn, 'maintainer'
            )
            self._add_triple_from_dict(
                dataset_dict, maintainer_details,
                VCARD.hasEmail, 'maintainer_email',
                _type=URIRef, value_modifier=self._add_mailto
            )

            # Add maintainer URL
            self._add_triple_from_dict(
                dataset_dict, maintainer_details,
                VCARD.hasURL, 'maintainer_url',
                _type=URIRef)

            # Add maintainer role
            g.add((maintainer_details, VCARD.role, URIRef(euro_dcat_ap_default_values['maintainer_role'])))

        # Resource author
        if any([
            self._get_dataset_value(dataset_dict, 'author'),
            self._get_dataset_value(dataset_dict, 'author_uri'),
            self._get_dataset_value(dataset_dict, 'author_email'),
            self._get_dataset_value(dataset_dict, 'author_url'),
        ]):
            author_uri = self._get_dataset_value(dataset_dict, 'author_uri')
            if author_uri:
                author_details = CleanedURIRef(author_uri)
            else:
                author_details = dataset_ref + '/author'
                
            g.add((author_details, RDF.type, VCARD.Organization))
            g.add((dataset_ref, DCT.creator, author_details))

            ## Add name & mail
            self._add_triple_from_dict(
                dataset_dict, author_details,
                VCARD.fn, 'author'
            )
            self._add_triple_from_dict(
                dataset_dict, author_details,
                VCARD.hasEmail, 'author_email',
                _type=URIRef, value_modifier=self._add_mailto
            )

            # Add author URL
            self._add_triple_from_dict(
                dataset_dict, author_details,
                VCARD.hasURL, 'author_url',
                _type=URIRef)

            # Add author role
            g.add((author_details, VCARD.role, URIRef(euro_dcat_ap_default_values['author_role'])))

        # Provenance: dataset dct:provenance dct:ProvenanceStatement
        provenance_details = dataset_ref + '/provenance'
        provenance_statement = self._get_dataset_value(dataset_dict, 'provenance')
        if provenance_statement:
            g.add((dataset_ref, DCT.provenance, provenance_details))
            g.add((provenance_details, RDF.type, DCT.ProvenanceStatement))
            
            if isinstance(provenance_statement, dict):
                self._add_multilang_triple(provenance_details, RDFS.label, provenance_statement)
            else:
                g.add((provenance_details, RDFS.label, Literal(provenance_statement)))

        # Publisher
        if any([
            self._get_dataset_value(dataset_dict, 'publisher_uri'),
            self._get_dataset_value(dataset_dict, 'publisher_name'),
            dataset_dict.get('organization'),
        ]):

            publisher_uri = self._get_dataset_value(dataset_dict, 'publisher_uri')
            publisher_uri_fallback = publisher_uri_organization_fallback(dataset_dict)
            publisher_name = self._get_dataset_value(dataset_dict, 'publisher_name')
            if publisher_uri:
                publisher_details = CleanedURIRef(publisher_uri)
            elif not publisher_name and publisher_uri_fallback:
                # neither URI nor name are available, use organization as fallback
                publisher_details = CleanedURIRef(publisher_uri_fallback)
            else:
                # No publisher_uri
                publisher_details = BNode()

            g.add((publisher_details, RDF.type, FOAF.Organization))
            g.add((dataset_ref, DCT.publisher, publisher_details))

            # In case no name and URI are available, again fall back to organization.
            # If no name but an URI is available, the name literal remains empty to
            # avoid mixing organization and dataset values.
            if not publisher_name and not publisher_uri and dataset_dict.get('organization'):
                publisher_name = dataset_dict['organization']['title']

            g.add((publisher_details, FOAF.name, Literal(publisher_name)))
            # TODO: It would make sense to fallback these to organization
            # fields but they are not in the default schema and the
            # `organization` object in the dataset_dict does not include
            # custom fields
            items = [
                ('publisher_email', FOAF.mbox, None, Literal),
                ('publisher_url', FOAF.homepage, None, URIRef),
                ('publisher_type', DCT.type, None, URIRefOrLiteral),
                ('publisher_identifier', DCT.identifier, None, URIRefOrLiteral)
            ]

            # Add publisher role
            g.add((publisher_details, VCARD.role, URIRef(euro_dcat_ap_default_values['publisher_role'])))

            self._add_triples_from_dict(dataset_dict, publisher_details, items)

        # TODO: Deprecated: https://semiceu.github.io/GeoDCAT-AP/drafts/latest/#deprecated-properties-for-period-of-time
        # Temporal
        '''        
        start = self._get_dataset_value(dataset_dict, 'temporal_start')
        end = self._get_dataset_value(dataset_dict, 'temporal_end')
        if start or end:
            temporal_extent = BNode()

            g.add((temporal_extent, RDF.type, DCT.PeriodOfTime))
            if start:
                self._add_date_triple(temporal_extent, SCHEMA.startDate, start)
            if end:
                self._add_date_triple(temporal_extent, SCHEMA.endDate, end)
            g.add((dataset_ref, DCT.temporal, temporal_extent))
        '''

        # Spatial
        spatial_text = self._get_dataset_value(dataset_dict, 'spatial_text')
        spatial_geom = self._get_dataset_value(dataset_dict, 'spatial')

        if spatial_text or spatial_geom:
            spatial_ref = self._get_or_create_spatial_ref(dataset_dict, dataset_ref)

            if spatial_text:
                g.add((spatial_ref, SKOS.prefLabel, Literal(spatial_text)))

            # Deprecated by dcat:bbox
            # if spatial_geom:
            #     self._add_spatial_value_to_graph(spatial_ref, LOCN.geometry, spatial_geom)

        # Coordinate Reference System
        if self._get_dataset_value(dataset_dict, 'reference_system'):
            crs_uri = self._get_dataset_value(dataset_dict, 'reference_system')
            crs_details = CleanedURIRef(crs_uri)
            g.add((crs_details, RDF.type, DCT.Standard))
            g.add((crs_details, DCT.type, CleanedURIRef(euro_dcat_ap_default_values['reference_system_type'])))
            g.add((dataset_ref, DCT.conformsTo, crs_details))

        # Use fallback license if set in config
        resource_license_fallback = None
        if toolkit.asbool(config.get(DISTRIBUTION_LICENSE_FALLBACK_CONFIG, False)):
            if 'license_id' in dataset_dict and isinstance(URIRefOrLiteral(dataset_dict['license_id']), URIRef):
                resource_license_fallback = dataset_dict['license_id']
            elif 'license_url' in dataset_dict and isinstance(URIRefOrLiteral(dataset_dict['license_url']), URIRef):
                resource_license_fallback = dataset_dict['license_url']

        # Resources
        for resource_dict in dataset_dict.get('resources', []):

            distribution = CleanedURIRef(resource_uri(resource_dict))

            g.add((dataset_ref, DCAT.distribution, distribution))

            g.add((distribution, RDF.type, DCAT.Distribution))

            #  Simple values
            items = [
                ('name', DCT.title, None, Literal),
                ('encoding', CNT.characterEncoding, None, Literal),
                ('description', DCT.description, None, Literal),
                ('status', ADMS.status, None, URIRefOrLiteral),
                ('license', DCT.license, None, URIRefOrLiteral),
                ('access_url', DCAT.accessURL, None, URIRef),
                ('download_url', DCAT.downloadURL, None, URIRef),
            ]

            self._add_triples_from_dict(resource_dict, distribution, items)

            #  Lists
            items = [
                ('documentation', FOAF.page, None, URIRefOrLiteral),
                ('language', DCT.language, None, URIRefOrLiteral),
                ('conforms_to', DCT.conformsTo, None, URIRef),
                ('metadata_profile', DCT.conformsTo, None, URIRef),
            ]
            self._add_list_triples_from_dict(resource_dict, distribution, items)

            # Set default license for distribution if needed and available
            if resource_license_fallback and not (distribution, DCT.license, None) in g:
                g.add((distribution, DCT.license, URIRefOrLiteral(resource_license_fallback)))

            # Format
            mimetype = resource_dict.get('mimetype')
            fmt = resource_dict.get('format').replace(" ", "")

            # IANA media types (either URI or Literal) should be mapped as mediaType.
            # In case format is available and mimetype is not set or identical to format,
            # check which type is appropriate.
            if mimetype:
                g.add((distribution, DCAT.mediaType, URIRefOrLiteral(mimetype)))
            elif fmt:
                mime_val = self._search_value_codelist(MD_FORMAT, fmt, "id", "media_type") or None
                if mime_val and mime_val != fmt:
                    g.add((distribution, DCAT.mediaType, URIRefOrLiteral(mime_val)))

            # Try to match format field
            fmt = self._search_value_codelist(MD_FORMAT, fmt, "label", "id") or fmt

            # Add format to graph
            if fmt:
                g.add((distribution, DCT['format'], URIRefOrLiteral(fmt)))

            # URL fallback and old behavior
            url = resource_dict.get('url')
            download_url = resource_dict.get('download_url')
            access_url = resource_dict.get('access_url')
            # Use url as fallback for access_url if access_url is not set and download_url is not equal
            if url and not access_url:
                if (not download_url) or (download_url and url != download_url):
                  self._add_triple_from_dict(resource_dict, distribution, DCAT.accessURL, 'url', _type=URIRef)

            # Dates
            items = [
                ('issued', DCT.issued, None, Literal),
                ('modified', DCT.modified, None, Literal),
            ]

            self._add_date_triples_from_dict(resource_dict, distribution, items)

            # DCAT-AP: http://publications.europa.eu/en/web/eu-vocabularies/at-dataset/-/resource/dataset/access-right
            access_rights = self._get_resource_value(resource_dict, 'rights')
            if access_rights and 'authority/access-right' in access_rights:
                access_rights_uri = URIRef(access_rights)
            else:
                access_rights_uri = URIRef(euro_dcat_ap_default_values['access_rights'])
            g.add((distribution, DCT.rights, access_rights_uri))

            # Numbers
            if resource_dict.get('size'):
                try:
                    g.add((distribution, DCAT.byteSize,
                           Literal(float(resource_dict['size']),
                                   datatype=XSD.decimal)))
                except (ValueError, TypeError):
                    g.add((distribution, DCAT.byteSize,
                           Literal(resource_dict['size'])))
            # Checksum
            if resource_dict.get('hash'):
                checksum = BNode()
                g.add((checksum, RDF.type, SPDX.Checksum))
                g.add((checksum, SPDX.checksumValue,
                       Literal(resource_dict['hash'],
                               datatype=XSD.hexBinary)))

                if resource_dict.get('hash_algorithm'):
                    g.add((checksum, SPDX.algorithm,
                           URIRefOrLiteral(resource_dict['hash_algorithm'])))

                g.add((distribution, SPDX.checksum, checksum))

    def graph_from_catalog(self, catalog_dict, catalog_ref):

        g = self.g

        for prefix, namespace in namespaces.items():
            g.bind(prefix, namespace)

        g.add((catalog_ref, RDF.type, DCAT.Catalog))
        
        # Basic fields
        license, publisher_identifier, access_rights, spatial_uri, language = [
            self._get_catalog_field(field_name='license_url'),
            self._get_catalog_field(field_name='publisher_identifier', fallback='publisher_uri'),
            self._get_catalog_field(field_name='access_rights'),
            self._get_catalog_field(field_name='spatial_uri'),
            self._search_value_codelist(MD_EU_LANGUAGES, config.get('ckan.locale_default'), "label","id") or euro_dcat_ap_default_values['language'],
            ]

        # Mandatory elements by NTI-RISP (datos.gob.es)
        items = [
            ('identifier', DCT.identifier, f'{config.get("ckan_url")}/catalog.rdf', Literal),
            ('title', DCT.title, config.get('ckan.site_title'), Literal),
            ('encoding', CNT.characterEncoding, 'UTF-8', Literal),
            ('description', DCT.description, config.get('ckan.site_description'), Literal),
            ('publisher_identifier', DCT.publisher, publisher_identifier, URIRef),
            ('language', DCT.language, language, URIRefOrLiteral),
            ('spatial_uri', DCT.spatial, spatial_uri, URIRefOrLiteral),
            ('theme_taxonomy', DCAT.themeTaxonomy, euro_dcat_ap_default_values['theme_taxonomy'], URIRef),
            ('homepage', FOAF.homepage, config.get('ckan_url'), URIRef),
            ('license', DCT.license, license, URIRef),
            ('conforms_to', DCT.conformsTo, euro_dcat_ap_default_values['conformance'], URIRef),
            ('access_rights', DCT.accessRights, access_rights, URIRefOrLiteral),
        ]
                 
        for item in items:
            key, predicate, fallback, _type = item
            value = catalog_dict.get(key, fallback) if catalog_dict else fallback
            if value:
                g.add((catalog_ref, predicate, _type(value)))

        # Dates
        modified = self._get_catalog_field(field_name='metadata_modified')
        issued = self._get_catalog_field(field_name='metadata_created', order='asc')
        if modified or issued or license:
            if modified:
                self._add_date_triple(catalog_ref, DCT.modified, modified)
            if issued:
                self._add_date_triple(catalog_ref, DCT.issued, issued)

class EuropeanDCATAP2Profile(EuropeanDCATAPProfile):
    '''
    An RDF profile based on the DCAT-AP 2 for data portals in Europe

    More information and specification:

    https://joinup.ec.europa.eu/asset/dcat_application_profile

    '''

    def parse_dataset(self, dataset_dict, dataset_ref):

        # call super method
        super(EuropeanDCATAP2Profile, self).parse_dataset(dataset_dict, dataset_ref)

        # Lists
        for key, predicate in (
            ('temporal_resolution', DCAT.temporalResolution),
            ('is_referenced_by', DCT.isReferencedBy),
            ('applicable_legislation', DCATAP.applicableLegislation),
            ('hvd_category', DCATAP.hvdCategory),
        ):
            values = self._object_value_list(dataset_ref, predicate)
            if values:
                dataset_dict['extras'].append({'key': key,
                                               'value': json.dumps(values)})
        # Temporal
        start, end = self._time_interval(dataset_ref, DCT.temporal, dcat_ap_version=2)
        if start:
            self._insert_or_update_temporal(dataset_dict, 'temporal_start', start)
        if end:
            self._insert_or_update_temporal(dataset_dict, 'temporal_end', end)

        # Spatial
        spatial = self._spatial(dataset_ref, DCT.spatial)
        for key in ('bbox', 'centroid'):
            self._add_spatial_to_dict(dataset_dict, key, spatial)

        # Spatial resolution in meters
        spatial_resolution_in_meters = self._object_value_int_list(
            dataset_ref, DCAT.spatialResolutionInMeters)
        if spatial_resolution_in_meters:
            dataset_dict['extras'].append({'key': 'spatial_resolution_in_meters',
                                           'value': json.dumps(spatial_resolution_in_meters)})

        # Resources
        for distribution in self._distributions(dataset_ref):
            distribution_ref = str(distribution)
            for resource_dict in dataset_dict.get('resources', []):
                # Match distribution in graph and distribution in resource dict
                if resource_dict and distribution_ref == resource_dict.get('distribution_ref'):
                    #  Simple values
                    for key, predicate in (
                            ('availability', DCATAP.availability),
                            ('compress_format', DCAT.compressFormat),
                            ('package_format', DCAT.packageFormat),
                            ):
                        value = self._object_value(distribution, predicate)
                        if value:
                            resource_dict[key] = value

                    #  Lists
                    for key, predicate in (
                            ('applicable_legislation', DCATAP.applicableLegislation),
                            ):
                        values = self._object_value_list(distribution, predicate)
                        if values:
                            resource_dict[key] = json.dumps(values)

                    # Access services
                        access_service_list = []

                        for access_service in self.g.objects(distribution, DCAT.accessService):
                            access_service_dict = {}

                            #  Simple values
                            for key, predicate in (
                                    ('availability', DCATAP.availability),
                                    ('title', DCT.title),
                                    ('endpoint_description', DCAT.endpointDescription),
                                    ('license', DCT.license),
                                    ('access_rights', DCT.accessRights),
                                    ('description', DCT.description),
                                    ):
                                value = self._object_value(access_service, predicate)
                                if value:
                                    access_service_dict[key] = value
                            #  List
                            for key, predicate in (
                                    ('endpoint_url', DCAT.endpointURL),
                                    ('serves_dataset', DCAT.servesDataset),
                                    ):
                                values = self._object_value_list(access_service, predicate)
                                if values:
                                    access_service_dict[key] = values

                            # Access service URI (explicitly show the missing ones)
                            access_service_dict['uri'] = (str(access_service)
                                    if isinstance(access_service, URIRef)
                                    else '')

                            # Remember the (internal) access service reference for referencing in
                            # further profiles, e.g. for adding more properties
                            access_service_dict['access_service_ref'] = str(access_service)

                            access_service_list.append(access_service_dict)

                        if access_service_list:
                            resource_dict['access_services'] = json.dumps(access_service_list)

        return dataset_dict

    def graph_from_dataset(self, dataset_dict, dataset_ref):

        # call super method
        super(EuropeanDCATAP2Profile, self).graph_from_dataset(dataset_dict, dataset_ref)
        
        # Lists
        for key, predicate, fallbacks, type, datatype in (
            ('temporal_resolution', DCAT.temporalResolution, None, Literal, XSD.duration),
            ('is_referenced_by', DCT.isReferencedBy, None, URIRefOrLiteral, None),
            ('applicable_legislation', DCATAP.applicableLegislation, None, URIRefOrLiteral, None),
            ('hvd_category', DCATAP.hvdCategory, None, URIRefOrLiteral, None),
        ):
            self._add_triple_from_dict(dataset_dict, dataset_ref, predicate, key, list_value=True,
                                       fallbacks=fallbacks, _type=type, _datatype=datatype)

        # Temporal
        start = self._get_dataset_value(dataset_dict, 'temporal_start')
        end = self._get_dataset_value(dataset_dict, 'temporal_end')
        if start or end:
            temporal_extent_dcat = BNode()

            self.g.add((temporal_extent_dcat, RDF.type, DCT.PeriodOfTime))
            if start:
                self._add_date_triple(temporal_extent_dcat, DCAT.startDate, start)
            if end:
                self._add_date_triple(temporal_extent_dcat, DCAT.endDate, end)
            self.g.add((dataset_ref, DCT.temporal, temporal_extent_dcat))

        # spatial
        spatial_bbox = self._get_dataset_value(dataset_dict, 'spatial_bbox') or self._get_dataset_value(dataset_dict, 'spatial')
        spatial_cent = self._get_dataset_value(dataset_dict, 'spatial_centroid')

        if spatial_bbox or spatial_cent:
            spatial_ref = self._get_or_create_spatial_ref(dataset_dict, dataset_ref)

            # _add_wkt_or_geojson_value_to_graph to comply with GeoDCAT-AP: Geometries MAY be provided in multiple encodings, but at least one of the following MUST be made available: GML and WKT.(https://semiceu.github.io/GeoDCAT-AP/drafts/latest/#bounding-box)
            if spatial_bbox:
                try:
                    self._add_wkt_or_geojson_value_to_graph(spatial_ref, DCAT.bbox, spatial_bbox)
                except:
                    self.g.add((spatial_ref, DCAT.bbox, Literal(spatial_bbox, datatype=GSP.geoJSONLiteral)))
            if spatial_cent:
                try:
                    self._add_wkt_or_geojson_value_to_graph(spatial_ref, DCAT.bbox, spatial_cent)
                except:
                    self.g.add((spatial_ref, DCAT.bbox, Literal(spatial_cent, datatype=GSP.geoJSONLiteral)))

        # Spatial resolution in meters
        spatial_resolution_in_meters = self._get_dataset_value(dataset_dict, 'spatial_resolution_in_meters')
        if spatial_resolution_in_meters:
            try:
                self.g.add((dataset_ref, DCAT.spatialResolutionInMeters,
                            Literal(float(spatial_resolution_in_meters), datatype=XSD.decimal)))
            except (ValueError, TypeError):
                self.g.add((dataset_ref, DCAT.spatialResolutionInMeters, Literal(spatial_resolution_in_meters)))

        # Resources
        for resource_dict in dataset_dict.get('resources', []):

            distribution = CleanedURIRef(resource_uri(resource_dict))

            #  Simple values
            items = [
                ('availability', DCATAP.availability, None, URIRefOrLiteral),
                ('compress_format', DCAT.compressFormat, None, URIRefOrLiteral),
                ('package_format', DCAT.packageFormat, None, URIRefOrLiteral)
            ]

            self._add_triples_from_dict(resource_dict, distribution, items)

            #  Lists
            items = [
                ('applicable_legislation', DCATAP.applicableLegislation, None, URIRefOrLiteral),
            ]
            self._add_list_triples_from_dict(resource_dict, distribution, items)

            try:
                access_service_list = json.loads(resource_dict.get('access_services', '[]'))
                # Access service
                for access_service_dict in access_service_list:

                    access_service_uri = access_service_dict.get('uri')
                    if access_service_uri:
                        access_service_node = CleanedURIRef(access_service_uri)
                    else:
                        access_service_node = BNode()
                        # Remember the (internal) access service reference for referencing in
                        # further profiles
                        access_service_dict['access_service_ref'] = str(access_service_node)

                    self.g.add((distribution, DCAT.accessService, access_service_node))

                    self.g.add((access_service_node, RDF.type, DCAT.DataService))

                     #  Simple values
                    items = [
                        ('availability', DCATAP.availability, None, URIRefOrLiteral),
                        ('license', DCT.license, None, URIRefOrLiteral),
                        ('title', DCT.title, None, Literal),
                        ('endpoint_description', DCAT.endpointDescription, None, Literal),
                        ('description', DCT.description, None, Literal),
                    ]

                    self._add_triples_from_dict(access_service_dict, access_service_node, items)

                    #  Lists
                    items = [
                        ('endpoint_url', DCAT.endpointURL, None, URIRefOrLiteral),
                        ('serves_dataset', DCAT.servesDataset, None, URIRefOrLiteral),
                    ]
                    self._add_list_triples_from_dict(access_service_dict, access_service_node, items)

                    # DCAT-AP: http://publications.europa.eu/en/web/eu-vocabularies/at-dataset/-/resource/dataset/access-right
                    access_rights = self._get_resource_value(resource_dict, 'access_rights')
                    if access_rights and 'authority/access-right' in access_rights:
                        access_rights_uri = URIRef(access_rights)
                    else:
                        access_rights_uri = URIRef(euro_dcat_ap_default_values['access_rights'])
                    self.g.add((distribution, DCT.accessRights, access_rights_uri))

                if access_service_list:
                    resource_dict['access_services'] = json.dumps(access_service_list)
            except ValueError:
                pass

    def graph_from_catalog(self, catalog_dict, catalog_ref):

        # call super method
        super(EuropeanDCATAP2Profile, self).graph_from_catalog(catalog_dict, catalog_ref)

class SpanishDCATProfile(EuropeanDCATAPProfile):
    '''
    An RDF profile based on the DCAT NTI-RISP profile for data portals in Spain

    Default values for some fields:
    
    ckanext-dcat/ckanext/dcat/profiles_default.py

    More information and specification:

    https://datos.gob.es/es/documentacion/guia-de-aplicacion-de-la-norma-tecnica-de-interoperabilidad-de-reutilizacion-de
    
    https://datos.gob.es/es/documentacion/guias-de-datosgobes
    
    https://datos.gob.es/es/documentacion/norma-tecnica-de-interoperabilidad-de-reutilizacion-de-recursos-de-informacion

    '''
    def parse_dataset(self, dataset_dict, dataset_ref):
        """
        Parses a CKAN dataset dictionary and generates an RDF graph.

        Args:
            dataset_dict (dict): The dictionary containing the dataset metadata.
            dataset_ref (URIRef): The URI of the dataset in the RDF graph.

        Returns:
            dict: The updated dataset dictionary with the RDF metadata.
        """
        # call super method
        super(SpanishDCATProfile, self).parse_dataset(dataset_dict, dataset_ref)

        # Lists
        for key, predicate in (
            ('theme_es', DCAT.theme),
            ('temporal_resolution', DCAT.temporalResolution),
            ('is_referenced_by', DCT.isReferencedBy),
        ):
            values = self._object_value_list(dataset_ref, predicate)
            if values:
                dataset_dict['extras'].append({'key': key,
                                               'value': json.dumps(values)})
        # Temporal
        start, end = self._time_interval(dataset_ref, DCT.temporal, dcat_ap_version=2)
        if start:
            self._insert_or_update_temporal(dataset_dict, 'temporal_start', start)
        if end:
            self._insert_or_update_temporal(dataset_dict, 'temporal_end', end)

        # Spatial
        spatial = self._spatial(dataset_ref, DCT.spatial)
        for key in ('bbox', 'centroid'):
            self._add_spatial_to_dict(dataset_dict, key, spatial)

        # Resources
        for distribution in self._distributions(dataset_ref):
            distribution_ref = str(distribution)
            for resource_dict in dataset_dict.get('resources', []):
                # Match distribution in graph and distribution in resource dict
                if resource_dict and distribution_ref == resource_dict.get('distribution_ref'):
                    #  Simple values
                    for key, predicate in (
                            ('availability', DCATAP.availability),
                            ('compress_format', DCAT.compressFormat),
                            ('package_format', DCAT.packageFormat),
                            ):
                        value = self._object_value(distribution, predicate)
                        if value:
                            resource_dict[key] = value

                    # Access services
                        access_service_list = []

                        for access_service in self.g.objects(distribution, DCAT.accessService):
                            access_service_dict = {}

                            #  Simple values
                            for key, predicate in (
                                    ('availability', DCATAP.availability),
                                    ('title', DCT.title),
                                    ('endpoint_description', DCAT.endpointDescription),
                                    ('license', DCT.license),
                                    ('access_rights', DCT.accessRights),
                                    ('description', DCT.description),
                                    ):
                                value = self._object_value(access_service, predicate)
                                if value:
                                    access_service_dict[key] = value
                            #  List
                            for key, predicate in (
                                    ('endpoint_url', DCAT.endpointURL),
                                    ('serves_dataset', DCAT.servesDataset),
                                    ('resource_relation', DCT.relation),
                                    ):
                                values = self._object_value_list(access_service, predicate)
                                if values:
                                    access_service_dict[key] = values

                            # Access service URI (explicitly show the missing ones)
                            access_service_dict['uri'] = (str(access_service)
                                    if isinstance(access_service, URIRef)
                                    else '')

                            # Remember the (internal) access service reference for referencing in
                            # further profiles, e.g. for adding more properties
                            access_service_dict['access_service_ref'] = str(access_service)

                            access_service_list.append(access_service_dict)

                        if access_service_list:
                            resource_dict['access_services'] = json.dumps(access_service_list)

        return dataset_dict

    def graph_from_dataset(self, dataset_dict, dataset_ref):
        """
        Generates an RDF graph from a dataset dictionary.

        Args:
            dataset_dict (dict): The dictionary containing the dataset metadata.
            dataset_ref (URIRef): The URI of the dataset in the RDF graph.

        Returns:
            None
        """
        
        # Namespaces
        self._bind_namespaces()
        
        g = self.g

        for prefix, namespace in namespaces.items():
            g.bind(prefix, namespace)

        g.add((dataset_ref, RDF.type, DCAT.Dataset))

        # Translated fields
        items = [(
            default_translated_fields[key]['field_name'],
            default_translated_fields[key]['rdf_predicate'],
            default_translated_fields[key]['fallbacks'],
            default_translated_fields[key]['_type'],
            default_translated_fields[key]['required_lang']
            )
            for key in default_translated_fields_spain_dcat
        ]
        self._add_triples_from_dict(dataset_dict, dataset_ref, items, multilang=True)


        # Basic elements (Title, description, Dates)
        basic_elements = [
            ('url', DCAT.landingPage, None, URIRef),
        ]
        
        dates = [
            ('created', DCT.created, ['metadata_created'], Literal),
            ('issued', DCT.issued, ['metadata_created'], Literal),
            ('modified', DCT.modified, ['metadata_modified'], Literal),
            ('valid', DCT.valid, None, Literal),
        ]
        
        self._add_date_triples_from_dict(dataset_dict, dataset_ref, dates)
        self._add_list_triples_from_dict(dataset_dict, dataset_ref, basic_elements)
        
        # NTI-RISP Core elements
        items = {
            'notes': (DCT.description, spain_dcat_default_values['notes']),
            'conforms_to_es': (DCT.conformsTo, spain_dcat_default_values['conformance_es']),
            'identifier_uri': (DCT.identifier, dataset_ref),
            'license_url': (DCT.license, spain_dcat_default_values['license_url']),
            'language_code': (DC.language, spain_dcat_default_values['language_code'] or config.get('ckan.locale_default')),
            'spatial_uri': (DCT.spatial, spain_dcat_default_values['spatial_uri']),
            'publisher_identifier_es': (DCT.publisher,  spain_dcat_default_values['publisher_identifier'] or dataset_dict['publisher_identifier']),
        }

        self._add_dataset_triples_from_dict(dataset_dict, dataset_ref, items)      

        # Lists
        dataset_dict['tags'] = [tag['name'].replace(" ", "").lower() for tag in dataset_dict.get('tags', [])]

        # Lists NTI-RISP Core
        items_list = [
            ('reference', DCT.references, None, URIRefOrLiteral),
            ('tags', DCAT.keyword, None, Literal),
            ('theme_es', DCAT.theme, spain_dcat_default_values['theme_es'], URIRefOrLiteral),
        ]

        #Lists DCAT-AP Extension
        items_dcatap_list = [
            ('conforms_to', DCT.conformsTo, None, URIRef),
            ('metadata_profile', DCT.conformsTo, None, URIRef),
        ]

        items_list.extend(items_dcatap_list)
        self._add_list_triples_from_dict(dataset_dict, dataset_ref, items_list)
         
        #FIXME Frequency - Frecuencia ('frequency', DCT.accrualPeriodicity, None, URIRefOrLiteral): https://github.com/ctt-gob-es/datos.gob.es/blob/30c4a0d97356e0caf948aff2bb74790f4885c67f/ckan/ckanext-dge-harvest/ckanext/dge_harvest/profiles.py#L2508

        # Temporal - Cobertura temporal
        self._temporal_graph(dataset_dict, dataset_ref)
    
        # Use fallback license if set in config
        resource_license_fallback = None
        if toolkit.asbool(config.get(DISTRIBUTION_LICENSE_FALLBACK_CONFIG, False)):
            if 'license_id' in dataset_dict and isinstance(URIRefOrLiteral(dataset_dict['license_id']), URIRef):
                resource_license_fallback = dataset_dict['license_id']
            elif 'license_url' in dataset_dict and isinstance(URIRefOrLiteral(dataset_dict['license_url']), URIRef):
                resource_license_fallback = dataset_dict['license_url']
        
        # Resources
        for resource_dict in dataset_dict.get('resources', []):

            distribution = CleanedURIRef(resource_uri(resource_dict))

            g.add((dataset_ref, DCAT.distribution, distribution))

            g.add((distribution, RDF.type, DCAT.Distribution))
            
            # NTI-RISP Core elements
            items = {
                'url': (DCAT.accessURL, None),
                'name': (DCT.title, None),
                'description': (DCT.description, spain_dcat_default_values['description']),
                'language_code': (DC.language, spain_dcat_default_values['language_code'] or config.get('ckan.locale_default')),
                'license': (DCT.license, spain_dcat_default_values['license']),
                'identifier_uri': (DCT.identifier, distribution),
                'size': (DCT.byteSize, None),
            }

            self._add_dataset_triples_from_dict(resource_dict, distribution, items)   

            # Lists
            items_lists = [
                ('resource_relation', DCT.relation, None, URIRef),
            ]

            self._add_list_triples_from_dict(resource_dict, distribution, items_lists)

            # Format/Mimetype - Formato de la distribución
            self._distribution_format(resource_dict, distribution)
    
    def graph_from_catalog(self, catalog_dict, catalog_ref):
        """
        Adds the metadata of a CKAN catalog to the RDF graph.

        Args:
            catalog_dict (dict): A dictionary containing the metadata of the catalog.
            catalog_ref (URIRef): The URI of the catalog in the RDF graph.

        Returns:
            None
        """
        g = self.g

        for prefix, namespace in namespaces.items():
            g.bind(prefix, namespace)

        g.add((catalog_ref, RDF.type, DCAT.Catalog))
        
        # Basic fields
        license, publisher_identifier, access_rights, spatial_uri, language_code = [
            self._get_catalog_field(field_name='license_url', fallback='license_id', default_values_dict=spain_dcat_default_values),
            spain_dcat_default_values['publisher_identifier'] or self._get_catalog_field(field_name='publisher_identifier', default_values_dict=spain_dcat_default_values),
            self._get_catalog_field(field_name='access_rights', default_values_dict=spain_dcat_default_values),
            self._get_catalog_field(field_name='spatial_uri', default_values_dict=spain_dcat_default_values),
            spain_dcat_default_values['language_code'] or config.get('ckan.locale_default')
            ]

        # Mandatory elements by NTI-RISP (datos.gob.es)
        items_core = [
            ('title', DCT.title, config.get('ckan.site_title'), Literal),
            ('description', DCT.description, config.get('ckan.site_description'), Literal),
            ('publisher_identifier', DCT.publisher, publisher_identifier, URIRef),
            ('identifier', DCT.identifier, f'{config.get("ckan_url")}/catalog.rdf', Literal),
            ('encoding', CNT.characterEncoding, 'UTF-8', Literal),
            ('language_code', DC.language, language_code, URIRefOrLiteral),
            ('spatial_uri', DCT.spatial, spatial_uri, URIRefOrLiteral),
            ('theme_taxonomy', DCAT.themeTaxonomy, spain_dcat_default_values['theme_taxonomy'], URIRef),
            ('homepage', FOAF.homepage, config.get('ckan_url'), URIRef),
        ]
        
        # DCAT-AP extension
        items_dcatap = [
            ('license', DCT.license, license, URIRef),
            ('conforms_to', DCT.conformsTo, spain_dcat_default_values['conformance_es'], URIRef),
            ('access_rights', DCT.accessRights, access_rights, URIRefOrLiteral),
        ]
         
        items_core.extend(items_dcatap)
         
        for item in items_core:
            key, predicate, fallback, _type = item
            value = catalog_dict.get(key, fallback) if catalog_dict else fallback
            if value:
                g.add((catalog_ref, predicate, _type(value)))

        #TODO: Tamaño del catálogo - dct:extent

        # Dates
        modified = self._get_catalog_field(field_name='metadata_modified', default_values_dict=spain_dcat_default_values)
        issued = self._get_catalog_field(field_name='metadata_created', default_values_dict=spain_dcat_default_values, order='asc')
        if modified or issued or license:
            if modified:
                self._add_date_triple(catalog_ref, DCT.modified, modified)
            if issued:
                self._add_date_triple(catalog_ref, DCT.issued, issued)

    def _add_dataset_triples_from_dict(self, dataset_dict, dataset_ref, items):
        """Adds triples to the RDF graph for the given dataset.

        Args:
            dataset_dict (dict): A dictionary containing the dataset metadata.
            dataset_ref (rdflib.URIRef): The URI reference for the dataset.
            items (dict): A dictionary containing the keys and values for the metadata items.

        Returns:
            None

        """
        
        for key, (predicate, default_value) in items.items():
            value = dataset_dict.get(key, default_value)
            if value is not None:
                self.g.add((dataset_ref, predicate, URIRefOrLiteral(value)))

    def _bind_namespaces(self):
        self.g.namespace_manager.bind('schema', namespaces['schema'], replace=True)

    def _temporal_graph(self, dataset_dict, dataset_ref):
        """Adds the dct:temporal triple to the RDF graph for the given dataset.

        Args:
            dataset_dict (dict): A dictionary containing the dataset metadata.
            dataset_ref (rdflib.URIRef): The URI reference for the dataset.

        Returns:
            None

        """
        # Temporal
        start = self._get_dataset_value(dataset_dict, 'temporal_start')
        end = self._get_dataset_value(dataset_dict, 'temporal_end')

        uid = 1

        temporal_extent = URIRef(
            "%s/%s-%s" % (dataset_ref, 'PeriodOfTime', uid))
        self.g.add((temporal_extent, RDF.type, DCT.PeriodOfTime))
        if start:
            self._add_date_triple(
                temporal_extent, SCHEMA.startDate, start)
        if end:
            self._add_date_triple(
                temporal_extent, SCHEMA.endDate, end)
        self.g.add((dataset_ref, DCT.temporal, temporal_extent))

    def _distribution_format(self, resource_dict, distribution):
        """
        Generates an RDF triple for the format of a resource.

        Args:
            mime_type (str): The MIME type of the format.
            label (str): The label of the format.

        Returns:
            str: The RDF triple for the format.
        """
        resource_format = resource_dict.get('format', spain_dcat_default_values['format_es'])

        format_es = self._search_value_codelist(MD_ES_FORMATS, resource_format, 'label','label', False) or spain_dcat_default_values['format_es']
        mime_type = self._search_value_codelist(MD_ES_FORMATS, format_es, 'label','id') or self.spain_dcat_default_values['mimetype_es']

        if format_es:
            imt = URIRef("%s/format" % distribution)
            self.g.add((imt, RDF.type, DCT.IMT))
            self.g.add((distribution, DCT['format'], imt))
            self.g.add((imt, RDFS.label, Literal(format_es)))

        if mime_type:
            self.g.add((imt, RDF.value, Literal(mime_type)))

    def _check_resource_url(self, resource_url, distribution):
        """
        Verifies if the URL of a resource is a download URL and adds the corresponding triple to the RDF graph.

        Args:
            resource_url (str): The URL of the resource.
            distribution (URIRef): The URI of the distribution in the RDF graph.

        Returns:
            None
        """
        
        download_keywords = ['descarga', 'download', 'get', 'file', 'data', 'archive', 'zip', 'rar', 'tar', 'gz', 'tgz', '7z', 'exe', 'msi', 'dmg', 'pkg', 'deb', 'rpm', 'iso', 'img', 'bin', 'cue', 'torrent', 'magnet', 'shp', 'gpkg', 'tiff']

        if any(keyword in resource_url.lower() for keyword in download_keywords):
            self.g.add((distribution, DCAT.downloadURL, URIRef(resource_url)))
        else:
            self.g.add((distribution, DCAT.accessURL, URIRef(resource_url)))

#TODO: Spanish profile GeoDCAT-AP extension
class SpanishDCATAPProfile(SpanishDCATProfile):
    '''
    An RDF profile based on the DCAT-AP to improve NTI-RISP profile for data portals in Spain

    More information and specification:

    https://semiceu.github.io/DCAT-AP/releases/3.0.0/

    '''
    def parse_dataset(self, dataset_dict, dataset_ref):
        """
        Parses a CKAN dataset dictionary and generates an RDF graph.

        Args:
            dataset_dict (dict): The dictionary containing the dataset metadata.
            dataset_ref (URIRef): The URI of the dataset in the RDF graph.

        Returns:
            dict: The updated dataset dictionary with the RDF metadata.
        """
        # call super method
        super(SpanishDCATAPProfile, self).parse_dataset(dataset_dict, dataset_ref)

        # Basic fields
        for key, predicate in (
                ('access_rights', DCT.accessRights),
                ('availability', DCATAP.availability),
                ):
            value = self._object_value(dataset_ref, predicate)
            if value:
                dataset_dict[key] = value

        # Lists
        for key, predicate in (
            ('temporal_resolution', DCAT.temporalResolution),
            ('is_referenced_by', DCT.isReferencedBy),
            ('applicable_legislation', DCATAP.applicableLegislation),
            ('hvd_category', DCATAP.hvdCategory),
        ):
            values = self._object_value_list(dataset_ref, predicate)
            if values:
                dataset_dict['extras'].append({'key': key,
                                               'value': json.dumps(values)})

        # Spatial resolution in meters
        spatial_resolution_in_meters = self._object_value_int_list(
            dataset_ref, DCAT.spatialResolutionInMeters)
        if spatial_resolution_in_meters:
            dataset_dict['extras'].append({'key': 'spatial_resolution_in_meters',
                                           'value': json.dumps(spatial_resolution_in_meters)})

    def graph_from_dataset(self, dataset_dict, dataset_ref):
        """
        Generates an RDF graph from a dataset dictionary.

        Args:
            dataset_dict (dict): The dictionary containing the dataset metadata.
            dataset_ref (URIRef): The URI of the dataset in the RDF graph.

        Returns:
            None
        """
        
        # call super method
        super(SpanishDCATAPProfile, self).graph_from_dataset(dataset_dict, dataset_ref)

        # DCAT-AP Extension
        geodcatap_items = {
            'availability': (DCATAP.availability, spain_dcat_default_values['availability']),
        }

        self._add_dataset_triples_from_dict(dataset_dict, dataset_ref, geodcatap_items)    

        # Lists
        for key, predicate in (
            ('is_referenced_by', DCT.isReferencedBy),
            ('applicable_legislation', DCATAP.applicableLegislation),
            ('hvd_category', DCATAP.hvdCategory),
        ):
            values = self._object_value_list(dataset_ref, predicate)
            if values:
                dataset_dict['extras'].append({'key': key,
                                               'value': json.dumps(values)})
        # Spatial resolution in meters
        spatial_resolution_in_meters = self._get_dataset_value(dataset_dict, 'spatial_resolution_in_meters')
        if spatial_resolution_in_meters:
            try:
                self.g.add((dataset_ref, DCAT.spatialResolutionInMeters,
                            Literal(float(spatial_resolution_in_meters), datatype=XSD.decimal)))
            except (ValueError, TypeError):
                self.g.add((dataset_ref, DCAT.spatialResolutionInMeters, Literal(spatial_resolution_in_meters)))

        # Access Rights
        # DCAT-AP: http://publications.europa.eu/en/web/eu-vocabularies/at-dataset/-/resource/dataset/access-right
        if self._get_dataset_value(dataset_dict, 'access_rights') and 'authority/access-right' in self._get_dataset_value(dataset_dict, 'access_rights'):
                self.g.add((dataset_ref, DCT.accessRights, URIRef(self._get_dataset_value(dataset_dict, 'access_rights'))))
        else:
            self.g.add((dataset_ref, DCT.accessRights, URIRef(euro_dcat_ap_default_values['access_rights'])))

    def graph_from_catalog(self, catalog_dict, catalog_ref):
        """
        Adds the metadata of a CKAN catalog to the RDF graph.

        Args:
            catalog_dict (dict): A dictionary containing the metadata of the catalog.
            catalog_ref (URIRef): The URI of the catalog in the RDF graph.

        Returns:
            None
        """

        # call super method
        super(SpanishDCATAPProfile, self).graph_from_catalog(catalog_dict, catalog_ref)

class SchemaOrgProfile(RDFProfile):
    '''
    An RDF profile based on the schema.org Dataset

    More information and specification:

    http://schema.org/Dataset

    Mapping between schema.org Dataset and DCAT:

    https://www.w3.org/wiki/WebSchemas/Datasets
    '''
    def graph_from_dataset(self, dataset_dict, dataset_ref):

        g = self.g

        # Namespaces
        self._bind_namespaces()

        g.add((dataset_ref, RDF.type, SCHEMA.Dataset))

        # Basic fields
        self._basic_fields_graph(dataset_ref, dataset_dict)

        # Catalog
        self._catalog_graph(dataset_ref, dataset_dict)

        # Groups
        self._groups_graph(dataset_ref, dataset_dict)

        # Tags
        self._tags_graph(dataset_ref, dataset_dict)

        #  Lists
        self._list_fields_graph(dataset_ref, dataset_dict)

        # Publisher
        self._publisher_graph(dataset_ref, dataset_dict)

        # Temporal
        self._temporal_graph(dataset_ref, dataset_dict)

        # Spatial
        self._spatial_graph(dataset_ref, dataset_dict)

        # Resources
        self._resources_graph(dataset_ref, dataset_dict)

        # Additional fields
        self.additional_fields(dataset_ref, dataset_dict)

    def additional_fields(self, dataset_ref, dataset_dict):
        '''
        Adds any additional fields.

        For a custom schema you should extend this class and
        implement this method.
        '''
        pass

    def _add_date_triple(self, subject, predicate, value, _type=Literal):
        '''
        Adds a new triple with a date object

        Dates are parsed using dateutil, and if the date obtained is correct,
        added to the graph as an SCHEMA.DateTime value.

        If there are parsing errors, the literal string value is added.
        '''
        if not value:
            return
        try:
            default_datetime = datetime.datetime(1, 1, 1, 0, 0, 0)
            _date = parse_date(value, default=default_datetime)

            self.g.add((subject, predicate, _type(_date.isoformat())))
        except ValueError:
            self.g.add((subject, predicate, _type(value)))

    def _bind_namespaces(self):
        self.g.namespace_manager.bind('schema', namespaces['schema'], replace=True)

    def _basic_fields_graph(self, dataset_ref, dataset_dict):
        items = [
            ('identifier', SCHEMA.identifier, None, Literal),
            ('title', SCHEMA.name, None, Literal),
            ('notes', SCHEMA.description, None, Literal),
            ('version', SCHEMA.version, ['dcat_version'], Literal),
            ('created', SCHEMA.dateCreated, ['metadata_created'], Literal),
            ('issued', SCHEMA.datePublished, ['metadata_created'], Literal),
            ('modified', SCHEMA.dateModified, ['metadata_modified'], Literal),
            ('license', SCHEMA.license, ['license_url', 'license_title'], Literal),
        ]
        self._add_triples_from_dict(dataset_dict, dataset_ref, items)

        items = [
            ('created', SCHEMA.dateCreated, ['metadata_created'], Literal),
            ('issued', SCHEMA.datePublished, ['metadata_created'], Literal),
            ('modified', SCHEMA.dateModified, ['metadata_modified'], Literal),
        ]

        self._add_date_triples_from_dict(dataset_dict, dataset_ref, items)

        # Dataset URL
        dataset_url = url_for('dataset.read',
                              id=dataset_dict['name'],
                              _external=True)
        self.g.add((dataset_ref, SCHEMA.url, Literal(dataset_url)))

    def _catalog_graph(self, dataset_ref, dataset_dict):
        data_catalog = BNode()
        self.g.add((dataset_ref, SCHEMA.includedInDataCatalog, data_catalog))
        self.g.add((data_catalog, RDF.type, SCHEMA.DataCatalog))
        self.g.add((data_catalog, SCHEMA.name, Literal(config.get('ckan.site_title'))))
        self.g.add((data_catalog, SCHEMA.description, Literal(config.get('ckan.site_description'))))
        self.g.add((data_catalog, SCHEMA.url, Literal(config.get('ckan.site_url'))))

    def _groups_graph(self, dataset_ref, dataset_dict):
        for group in dataset_dict.get('groups', []):
            group_url = url_for(controller='group',
                                action='read',
                                id=group.get('id'),
                                _external=True)
            about = BNode()

            self.g.add((about, RDF.type, SCHEMA.Thing))

            self.g.add((about, SCHEMA.name, Literal(group['name'])))
            self.g.add((about, SCHEMA.url, Literal(group_url)))

            self.g.add((dataset_ref, SCHEMA.about, about))

    def _tags_graph(self, dataset_ref, dataset_dict):
        for tag in dataset_dict.get('tags', []):
            self.g.add((dataset_ref, SCHEMA.keywords, URIRefOrLiteral(tag['name'])))

    def _list_fields_graph(self, dataset_ref, dataset_dict):
        items = [
            ('language', SCHEMA.inLanguage, None, Literal),
        ]
        self._add_list_triples_from_dict(dataset_dict, dataset_ref, items)

    def _publisher_graph(self, dataset_ref, dataset_dict):
        if any([
            self._get_dataset_value(dataset_dict, 'publisher_uri'),
            self._get_dataset_value(dataset_dict, 'publisher_name'),
            dataset_dict.get('organization'),
        ]):

            publisher_uri = self._get_dataset_value(dataset_dict, 'publisher_uri')
            publisher_uri_fallback = publisher_uri_organization_fallback(dataset_dict)
            publisher_name = self._get_dataset_value(dataset_dict, 'publisher_name')
            if publisher_uri:
                publisher_details = CleanedURIRef(publisher_uri)
            elif not publisher_name and publisher_uri_fallback:
                # neither URI nor name are available, use organization as fallback
                publisher_details = CleanedURIRef(publisher_uri_fallback)
            else:
                # No publisher_uri
                publisher_details = BNode()

            self.g.add((publisher_details, RDF.type, SCHEMA.Organization))
            self.g.add((dataset_ref, SCHEMA.publisher, publisher_details))

            # In case no name and URI are available, again fall back to organization.
            # If no name but an URI is available, the name literal remains empty to
            # avoid mixing organization and dataset values.
            if not publisher_name and not publisher_uri and dataset_dict.get('organization'):
                publisher_name = dataset_dict['organization']['title']
            self.g.add((publisher_details, SCHEMA.name, Literal(publisher_name)))

            contact_point = BNode()
            self.g.add((contact_point, RDF.type, SCHEMA.ContactPoint))
            self.g.add((publisher_details, SCHEMA.contactPoint, contact_point))

            self.g.add((contact_point, SCHEMA.contactType, Literal('customer service')))

            publisher_url = self._get_dataset_value(dataset_dict, 'publisher_url')
            if not publisher_url and dataset_dict.get('organization'):
                publisher_url = dataset_dict['organization'].get('url') or config.get('ckan.site_url')

            self.g.add((contact_point, SCHEMA.url, Literal(publisher_url)))
            items = [
                ('publisher_email', SCHEMA.email, ['contact_email', 'maintainer_email', 'author_email'], Literal),
                ('publisher_name', SCHEMA.name, ['contact_name', 'maintainer', 'author'], Literal),
            ]

            self._add_triples_from_dict(dataset_dict, contact_point, items)

    def _temporal_graph(self, dataset_ref, dataset_dict):
        start = self._get_dataset_value(dataset_dict, 'temporal_start')
        end = self._get_dataset_value(dataset_dict, 'temporal_end')
        if start or end:
            if start and end:
                self.g.add((dataset_ref, SCHEMA.temporalCoverage, Literal('%s/%s' % (start, end))))
            elif start:
                self._add_date_triple(dataset_ref, SCHEMA.temporalCoverage, start)
            elif end:
                self._add_date_triple(dataset_ref, SCHEMA.temporalCoverage, end)

    def _spatial_graph(self, dataset_ref, dataset_dict):
        spatial_uri = self._get_dataset_value(dataset_dict, 'spatial_uri')
        spatial_text = self._get_dataset_value(dataset_dict, 'spatial_text')
        spatial_geom = self._get_dataset_value(dataset_dict, 'spatial')

        if spatial_uri or spatial_text or spatial_geom:
            if spatial_uri:
                spatial_ref = URIRef(spatial_uri)
            else:
                spatial_ref = BNode()

            self.g.add((spatial_ref, RDF.type, SCHEMA.Place))
            self.g.add((dataset_ref, SCHEMA.spatialCoverage, spatial_ref))

            if spatial_text:
                self.g.add((spatial_ref, SCHEMA.description, Literal(spatial_text)))

            if spatial_geom:
                geo_shape = BNode()
                self.g.add((geo_shape, RDF.type, SCHEMA.GeoShape))
                self.g.add((spatial_ref, SCHEMA.geo, geo_shape))

                # the spatial_geom typically contains GeoJSON
                self.g.add((geo_shape,
                       SCHEMA.polygon,
                       Literal(spatial_geom)))

    def _resources_graph(self, dataset_ref, dataset_dict):
        g = self.g
        for resource_dict in dataset_dict.get('resources', []):
            distribution = URIRef(resource_uri(resource_dict))
            g.add((dataset_ref, SCHEMA.distribution, distribution))
            g.add((distribution, RDF.type, SCHEMA.DataDownload))

            self._distribution_graph(distribution, resource_dict)

    def _distribution_graph(self, distribution, resource_dict):
        #  Simple values
        self._distribution_basic_fields_graph(distribution, resource_dict)

        # Lists
        self._distribution_list_fields_graph(distribution, resource_dict)

        # Format
        self._distribution_format_graph(distribution, resource_dict)

        # URL
        self._distribution_url_graph(distribution, resource_dict)

        # Numbers
        self._distribution_numbers_graph(distribution, resource_dict)

    def _distribution_basic_fields_graph(self, distribution, resource_dict):
        items = [
            ('name', SCHEMA.name, None, Literal),
            ('description', SCHEMA.description, None, Literal),
            ('license', SCHEMA.license, ['rights'], Literal),
        ]

        self._add_triples_from_dict(resource_dict, distribution, items)

        items = [
            ('issued', SCHEMA.datePublished, None, Literal),
            ('modified', SCHEMA.dateModified, None, Literal),
        ]

        self._add_date_triples_from_dict(resource_dict, distribution, items)

    def _distribution_list_fields_graph(self, distribution, resource_dict):
        items = [
            ('language', SCHEMA.inLanguage, None, Literal),
        ]
        self._add_list_triples_from_dict(resource_dict, distribution, items)

    def _distribution_format_graph(self, distribution, resource_dict):
        if resource_dict.get('format'):
            self.g.add((distribution, SCHEMA.encodingFormat,
                   Literal(resource_dict['format'])))
        elif resource_dict.get('mimetype'):
            self.g.add((distribution, SCHEMA.encodingFormat,
                   Literal(resource_dict['mimetype'])))

    def _distribution_url_graph(self, distribution, resource_dict):
        url = resource_dict.get('url')
        download_url = resource_dict.get('download_url')
        if download_url:
            self.g.add((distribution, SCHEMA.contentUrl, Literal(download_url)))
        if (url and not download_url) or (url and url != download_url):
            self.g.add((distribution, SCHEMA.url, Literal(url)))

    def _distribution_numbers_graph(self, distribution, resource_dict):
        if resource_dict.get('size'):
            self.g.add((distribution, SCHEMA.contentSize, Literal(resource_dict['size'])))