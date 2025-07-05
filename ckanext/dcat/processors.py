import sys
import argparse
import xml
import json
from pkg_resources import iter_entry_points

from ckantoolkit import config

import rdflib
import rdflib.parser
from rdflib import URIRef, BNode, Literal
from rdflib.namespace import Namespace, RDF

import ckan.plugins as p

from ckanext.dcat.utils import catalog_uri, dataset_uri, url_to_rdflib_format, DCAT_EXPOSE_SUBCATALOGS
from ckanext.dcat.profiles import DCAT, DCT, FOAF
from ckanext.dcat.exceptions import RDFProfileException, RDFParserException

HYDRA = Namespace('http://www.w3.org/ns/hydra/core#')
DCAT = Namespace("http://www.w3.org/ns/dcat#")

RDF_PROFILES_ENTRY_POINT_GROUP = 'ckan.rdf.profiles'
RDF_PROFILES_CONFIG_OPTION = 'ckanext.dcat.rdf.profiles'
COMPAT_MODE_CONFIG_OPTION = 'ckanext.dcat.compatibility_mode'

DEFAULT_RDF_PROFILES = ['euro_dcat_ap_2']


class RDFProcessor(object):

    def __init__(self, profiles=None, compatibility_mode=False):
        '''
        Creates a parser or serializer instance

        You can optionally pass a list of profiles to be used.

        In compatibility mode, some fields are modified to maintain
        compatibility with previous versions of the ckanext-dcat parsers
        (eg adding the `dcat_` prefix or storing comma separated lists instead
        of JSON dumps).

        '''
        if not profiles:
            profiles = config.get(RDF_PROFILES_CONFIG_OPTION, None)
            if profiles:
                profiles = profiles.split(' ')
            else:
                profiles = DEFAULT_RDF_PROFILES
        self._profiles = self._load_profiles(profiles)
        if not self._profiles:
            raise RDFProfileException(
                'No suitable RDF profiles could be loaded')

        if not compatibility_mode:
            compatibility_mode = p.toolkit.asbool(
                config.get(COMPAT_MODE_CONFIG_OPTION, False))
        self.compatibility_mode = compatibility_mode

        self.g = rdflib.ConjunctiveGraph()

    def _load_profiles(self, profile_names):
        '''
        Loads the specified RDF parser profiles

        These are registered on ``entry_points`` in setup.py, under the
        ``[ckan.rdf.profiles]`` group.

        Returns a list of loaded profiles
        '''
        profiles = []

        for profile_name in profile_names:
            for profile in iter_entry_points(
                    group=RDF_PROFILES_ENTRY_POINT_GROUP,
                    name=profile_name):
                profile_class = profile.resolve()
                # Set a reference to the profile name
                profile_class.name = profile.name
                profiles.append(profile_class)

        return profiles

    def _run_on_profiles(self, method_name, fallback=None, *args, **kwargs):
        '''
        Calls the same method on each profile

        Profiles are called in the same order they are defined.

        If a profile does not have the method, we move to the next one,
        except if fallback is provided, in which case we call the fallback
        method instead.

        We finish when the first profile returns a value.

        Returns the value from the first profile that returns something. This
        can be None, so if profiles want to explicitly mark that they didn't
        handle a particular call they should raise RDFProfileException.

        '''
        value = None
        for profile_class in self._profiles:
            profile = profile_class(self.g, self.compatibility_mode)
            if hasattr(profile, method_name):
                try:
                    value = getattr(profile, method_name)(*args, **kwargs)
                except RDFProfileException as e:
                    # We expect profiles to raise this exception if they can't
                    # handle a particular dataset for instance
                    pass
                if value is not None:
                    break
            else:
                if fallback and hasattr(profile, fallback):
                    try:
                        value = getattr(profile, fallback)(*args, **kwargs)
                    except RDFProfileException as e:
                        # We expect profiles to raise this exception if they
                        # can't handle a particular dataset for instance
                        pass
                    if value is not None:
                        break

        return value


class RDFParser(RDFProcessor):
    '''
    An RDF to CKAN parser based on rdflib

    Supports different profiles which are the ones that will generate
    CKAN dicts from the RDF graph.
    '''

    def _datasets(self):
        '''
        Generator that returns CKAN datasets parsed from the RDF graph

        Each dataset is passed to all the loaded profiles before being
        yielded, so it can be further modified by each one of them.

        Returns a dataset dict that can be passed to eg `package_create`
        or `package_update`
        '''
        for dataset_ref in self._dataset_refs():
            dataset_dict = {}
            for profile_class in self._profiles:
                profile = profile_class(self.g, self.compatibility_mode)
                profile.parse_dataset(dataset_dict, dataset_ref)

            yield dataset_dict

    def _dataset_refs(self):
        '''
        Returns a list of rdflib URIRefs that represent datasets in the graph

        Checks for datasets on the first profile that implements this method,
        otherwise defaults to all DCAT datasets.
        '''

        refs = self._run_on_profiles(
            'datasets',
            fallback='_datasets',
        )

        if not refs:
            # Get all DCAT datasets
            refs = [d for d in self.g.subjects(RDF.type, DCAT.Dataset)]

        return refs

    def datasets(self):
        '''
        Generator that returns CKAN datasets parsed from the RDF graph

        Each dataset is passed to all the loaded profiles before being
        yielded, so it can be further modified by each one of them.

        Returns a dataset dict that can be passed to eg `package_create`
        or `package_update`
        '''
        for dataset in self._datasets():
            yield dataset

    def parse(self, data, _format=None):
        '''
        Parses and RDF graph from a string, a file-like object or a URL

        It calls the rdflib parse function with the provided data and format.

        Data is a string with the serialized RDF graph, a file-like object or
        an URL.  If the data is an URL, the format can be guessed from
        the content type.

        _format is an optional string with the format that will be passed to
        rdflib), eg: csv, n3, xml, ttl...

        '''

        # Workaround for https://github.com/RDFLib/rdflib/issues/1484
        # Avoid interpreting strings like "N802" as numbers.
        # Only to be used when strictly necessary as it introduces a serious
        # performance penalty and makes float values like "5e-4" not being
        # parsed correctly
        if _format == 'csv':
            from rdflib.plugins.parsers.notation3 import ParserError as N3ParserError
            orig_function = rdflib.plugins.parsers.notation3.exponent_syntax

            def exponent_syntax(self, argstr, i, res):
                try:
                    return orig_function(self, argstr, i, res)
                except N3ParserError:
                    return -1
            rdflib.plugins.parsers.notation3.exponent_syntax = exponent_syntax

        _format = url_to_rdflib_format(_format)
        if _format == 'pretty-xml':
            _format = 'xml'

        try:
            self.g.parse(data=data, format=_format)
        # Apparently there is no single way of catching exceptions from all
        # rdflib parsers.
        except (SyntaxError,
                xml.sax.SAXParseException,
                rdflib.plugin.PluginException,
                rdflib.parser.ParserError) as e:

            raise RDFParserException(e)


class RDFSerializer(RDFProcessor):
    '''
    A CKAN to RDF serializer based on rdflib

    Supports different profiles which are the ones that will generate
    the RDF graph.
    '''

    def _add_datasets_to_graph(self, dataset_dicts, catalog_ref):
        '''
        Adds the given dataset dicts to the RDF graph, using the loaded
        profiles

        ``catalog_ref`` is an rdflib URIRef object that represents the
        catalog

        Returns a list of rdflib URIRef objects that represent the added
        datasets
        '''
        if not isinstance(dataset_dicts, list):
            dataset_dicts = [dataset_dicts]

        dataset_refs = []
        for dataset_dict in dataset_dicts:

            dataset_ref = URIRef(dataset_uri(dataset_dict))

            for profile_class in self._profiles:
                profile = profile_class(self.g, self.compatibility_mode)
                profile.graph_from_dataset(dataset_dict, dataset_ref)

            dataset_refs.append(dataset_ref)

            if catalog_ref:
                self.g.add((catalog_ref, DCAT.dataset, dataset_ref))

        return dataset_refs

    def _add_catalog_to_graph(self, catalog_ref=None, catalog_dict=None):
        '''
        Adds the catalog to the graph using the loaded profiles

        The class RDFProfile will be used by default if no profiles are
        provided.

        Returns the reference to the catalog, which can be used to add
        datasets to it.

        '''
        if not catalog_ref:
            catalog_ref = URIRef(catalog_uri())

        for profile_class in self._profiles:
            profile = profile_class(self.g, self.compatibility_mode)
            profile.graph_from_catalog(catalog_dict, catalog_ref)

        return catalog_ref

    def _add_pagination_to_graph(self, paging_info):
        '''
        Adds pagination triples to the graph using the loaded profiles

        The pagination information dict can contain the following:

        {
            'count': 2,
            'items_per_page': 1,
            'current': 'http://example.com/catalog/1',
            'first': 'http://example.com/catalog/1',
            'last': 'http://example.com/catalog/2',
            'next': 'http://example.com/catalog/2',
            'previous': None,
        }

        '''

        self._run_on_profiles(
            'graph_from_catalog_pagination',
            catalog_pagination=paging_info,
        )

    def serialize_dataset(self, dataset_dict, _format='xml'):
        '''
        Given a CKAN dataset dict, returns an RDF serialization

        The serialization format can be defined using the `_format` parameter.
        It must be one of the ones supported by rdflib, defaults to `xml`.

        Returns a string with the datasetd serialized in the requested format.
        '''

        self._add_datasets_to_graph(dataset_dict, catalog_ref=None)

        if _format == 'json-ld':
            _format = 'json-ld'

        return self.g.serialize(format=_format)

    def serialize_catalog(self, catalog_dict=None, dataset_dicts=None,
                          _format='xml', pagination_info=None):
        '''
        Returns an RDF serialization of the whole catalog

        `catalog_dict` can contain literal values for the dcat:Catalog class
        like `title`, `homepage`, etc. If not provided these would get default
        values from the CKAN config (eg from `ckan.site_title`).

        If passed a list of CKAN dataset dicts, these will be also added to
        the catalog.

        The serialization format can be defined using the `_format` parameter.
        It must be one of the ones supported by rdflib, defaults to `xml`.

        `pagination_info` may be a dict containing keys describing the results
        pagination. See the `_add_pagination_to_graph()` method for details.

        Returns a string with the catalog serialized in the requested format.

        '''

        catalog_ref = self._add_catalog_to_graph(catalog_ref=None,
                                                 catalog_dict=catalog_dict)

        if dataset_dicts:
            self._add_datasets_to_graph(dataset_dicts, catalog_ref)

        if pagination_info:
            self._add_pagination_to_graph(pagination_info)

        if _format == 'json-ld':
            _format = 'json-ld'

        return self.g.serialize(format=_format)


if __name__ == '__main__':

    parser = argparse.ArgumentParser(
        description='DCAT RDF - CKAN operations')
    parser.add_argument('mode',
                        default='consume',
                        help='''Operation mode.
                        `consume` parses DCAT RDF graphs to CKAN dataset JSON objects.
                        `produce` serializes CKAN dataset JSON objects into DCAT RDF.
                        ''')
    parser.add_argument('file', nargs='?', type=argparse.FileType('r'),
                        default=sys.stdin,
                        help='Input file. If omitted will read from stdin')
    parser.add_argument('-f', '--format',
                        dest='format',
                        default='xml',
                        help='''Serialization format (as understood by rdflib)
                        eg: xml, n3 ... Defaults to \'xml\'.''')
    parser.add_argument('-P', '--pretty',
                        dest='pretty',
                        action='store_true',
                        help='Make the output more human readable')
    parser.add_argument('-p', '--profile', nargs='*',
                        action='store',
                        help='RDF Profiles to use, defaults to euro_dcat_ap_2')
    parser.add_argument('-m', '--compat-mode',
                        dest='compat_mode',
                        action='store_true',
                        help='Enable compatibility mode')

    parser.add_argument('-s', '--subcatalogs',
                        dest='subcatalogs',
                        action='store_true',
                        help='''Enable subcatalogs handling.
                            This will store information about the origin catalog
                            when consuming and serialize the datasets when producing.''')

    args = parser.parse_args()

    if args.subcatalogs:
        config[DCAT_EXPOSE_SUBCATALOGS] = True

    contents = args.file.read()

    if args.mode == 'produce':
        if args.profile:
            profiles = args.profile
        else:
            profiles = None
        serializer = RDFSerializer(profiles=profiles,
                                   compatibility_mode=args.compat_mode)

        dataset = json.loads(contents)
        out = serializer.serialize_dataset(dataset, _format=args.format)
        print(out)
    else:
        parser = RDFParser(profiles=args.profile,
                           compatibility_mode=args.compat_mode)

        parser.parse(contents, _format=args.format)

        ckan_datasets = [d for d in parser.datasets()]

        indent = 4 if args.pretty else None
        print(json.dumps(ckan_datasets, indent=indent))