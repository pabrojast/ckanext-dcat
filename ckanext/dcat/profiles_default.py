from rdflib import URIRef, BNode, Literal
from rdflib.namespace import Namespace, RDF, XSD, SKOS, RDFS

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
"""
This file contains default values for the SpanishDCATProfile and EuropeanDCATAPProfile profiles.
Default values are used to fill in missing metadata in resources.
Default values can be overridden in resource-specific metadata.

metadata_field_names: dict
    A dictionary containing the metadata field names for each profile.

spain_dcat_default_values: dict
    A dictionary containing the default values for the SpanishDCATProfile.

euro_dcat_ap_default_values: dict
    A dictionary containing the default values for the EuropeanDCATAPProfile.
    
default_translated_fields: dict
    A dictionary containing the default translated fields for the ckanext-schemingdcat extension.
"""

# DCAT default elements by profile
metadata_field_names = {
    'euro_dcat_ap': {
        'theme': 'theme_eu',
    },
    'spain_dcat': {
        'theme': 'theme_es',
    }
}

# SpanishDCATProfile: Mandatory elements by NTI-RISP.
spain_dcat_default_values = {
    'availability': 'http://publications.europa.eu/resource/authority/planned-availability/AVAILABLE',
    'access_rights': 'http://publications.europa.eu/resource/authority/access-right/PUBLIC',
    'conformance_es': 'http://www.boe.es/eli/es/res/2013/02/19/(4)',
    'description': 'Recurso sin descripción.',
    'description_es': 'Recurso sin descripción.',
    'format_es': 'HTML',
    'language_code': 'es',
    'license': 'http://www.opendefinition.org/licenses/cc-by',
    'license_url': 'http://www.opendefinition.org/licenses/cc-by',
    'mimetype_es': 'text/html',
    'notes': 'Conjunto de datos sin descripción.',
    'notes_es': 'Conjunto de datos sin descripción.',
    'notes_en': 'Dataset without description.',
    'publisher_identifier': 'http://datos.gob.es/recurso/sector-publico/org/Organismo/EA0007777',
    'theme_es': 'http://datos.gob.es/kos/sector-publico/sector/sector-publico',
    'theme_eu': 'http://publications.europa.eu/resource/authority/data-theme/GOVE',
    'theme_taxonomy': 'http://datos.gob.es/kos/sector-publico/sector/',
    'spatial_uri': 'http://datos.gob.es/recurso/sector-publico/territorio/Pais/España',
}

# EuropeanDCATAPProfile: Mandatory catalog elements by DCAT-AP.
euro_dcat_ap_default_values = {
    'availability': 'http://publications.europa.eu/resource/authority/planned-availability/AVAILABLE',
    'access_rights': 'http://publications.europa.eu/resource/authority/access-right/PUBLIC',
    'author_role': 'http://inspire.ec.europa.eu/metadata-codelist/ResponsiblePartyRole/author',
    'conformance': 'http://data.europa.eu/930/',
    'contact_role': 'http://inspire.ec.europa.eu/metadata-codelist/ResponsiblePartyRole/pointOfContact',
    'description': 'Resource without description.',
    'description_en': 'Resource without description.',
    'description_es': 'Recurso sin descripción.',
    'license': 'http://www.opendefinition.org/licenses/cc-by',
    'license_url': 'http://www.opendefinition.org/licenses/cc-by',
    'maintainer_role': 'http://inspire.ec.europa.eu/metadata-codelist/ResponsiblePartyRole/custodian',
    'notes': 'Dataset without description.',
    'notes_es': 'Conjunto de datos sin descripción.',
    'notes_en': 'Dataset without description.',
    'publisher_identifier': 'http://datos.gob.es/recurso/sector-publico/org/Organismo/EA0007777',
    'publisher_role': 'http://inspire.ec.europa.eu/metadata-codelist/ResponsiblePartyRole/distributor',
    'reference_system_type': 'http://inspire.ec.europa.eu/glossary/SpatialReferenceSystem',
    'theme_es': 'http://datos.gob.es/kos/sector-publico/sector/sector-publico',
    'theme_eu': 'http://publications.europa.eu/resource/authority/data-theme/GOVE',
    'theme_taxonomy': 'http://publications.europa.eu/resource/authority/data-theme',
    'spatial_uri': 'http://publications.europa.eu/resource/authority/country/ESP',
}

# Default field_names to translated fields mapping (ckanext.schemingdcat:schemas)
default_translated_fields = {
    'title': 
        {
            'field_name': 'title_translated',
            'rdf_predicate': DCT.title,
            'fallbacks': ['title'],
            '_type': Literal,
            'required_lang': None
        },
    'notes': 
        {
            'field_name': 'notes_translated',
            'rdf_predicate': DCT.description,
            'fallbacks': ['notes'],
            '_type': Literal,
            'required_lang': None
        },
    'description': 
        {
            'field_name': 'description_translated',
            'rdf_predicate': DCT.description,
            'fallbacks': ['description'],
            '_type': Literal,
            'required_lang': None
        },
    'provenance': 
        {
            'field_name': 'provenance',
            'rdf_predicate': RDFS.label,
            'fallbacks': None,
            '_type': Literal,
            'required_lang': None
        },
    'version_notes': 
        {
            'field_name': 'version_notes',
            'rdf_predicate': ADMS.versionNotes,
            'fallbacks': None,
            '_type': Literal,
            'required_lang': None
        },
}

default_translated_fields_spain_dcat = {
    'title': 
        {
            'field_name': 'title_translated',
            'rdf_predicate': DCT.title,
            'fallbacks': ['title'],
            '_type': Literal,
            'required_lang': 'es',
        },
    'notes': 
        {
            'field_name': 'notes_translated',
            'rdf_predicate': DCT.description,
            'fallbacks': ['notes'],
            '_type': Literal,
            'required_lang': 'es',
        },
    'description': 
        {
            'field_name': 'description_translated',
            'rdf_predicate': DCT.description,
            'fallbacks': ['description'],
            '_type': Literal,
            'required_lang': 'es',
        }
}