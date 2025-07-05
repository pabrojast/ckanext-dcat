# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

**Testing:**
```bash
pytest --ckan-ini=test.ini ckanext/dcat/tests
```

**Running specific tests:**
```bash
pytest --ckan-ini=test.ini ckanext/dcat/tests/test_harvester.py
pytest --ckan-ini=test.ini ckanext/dcat/tests/test_euro_dcatap_2_profile_parse.py
```

**CLI tool for RDF processing:**
```bash
# Parse RDF to CKAN dataset JSON
python ckanext/dcat/processors.py consume examples/dataset.rdf -P

# Generate RDF from CKAN dataset
python ckanext/dcat/processors.py produce examples/ckan_dataset.json
```

## Architecture Overview

This extension provides DCAT (Data Catalog Vocabulary) support for CKAN with the following core components:

### Core Components

1. **Plugins** (`ckanext/dcat/plugins/__init__.py`):
   - `DCATPlugin`: Main plugin providing RDF endpoints and dataset serialization
   - `DCATJSONInterface`: JSON DCAT interface
   - `StructuredDataPlugin`: Adds schema.org structured data for Google Dataset Search

2. **Processors** (`ckanext/dcat/processors.py`):
   - `RDFParser`: Converts RDF serializations to CKAN dataset dictionaries
   - `RDFSerializer`: Converts CKAN datasets to RDF formats (XML, Turtle, JSON-LD)

3. **Profiles** (`ckanext/dcat/profiles.py`):
   - Define mapping between DCAT properties and CKAN fields
   - Available profiles: `euro_dcat_ap_2`, `euro_dcat_ap`, `spain_dcat`, `spain_dcat_ap`, `schemaorg`
   - Custom profiles can extend `RDFProfile` class

4. **Harvesters** (`ckanext/dcat/harvesters/`):
   - `DCATRDFHarvester`: Imports datasets from remote RDF sources
   - `DCATJSONHarvester`: Imports from JSON DCAT sources
   - Base harvester classes with extension points via `IDCATRDFHarvester` interface

### Key Features

- **RDF Endpoints**: Expose datasets as RDF in multiple formats (XML, Turtle, JSON-LD, N3)
- **Content Negotiation**: Accept headers for different RDF formats
- **Catalog Endpoint**: Paginated catalog-wide RDF export with Hydra vocabulary
- **Multilingual Support**: Handle multilingual RDF values
- **Custom Profiles**: Extensible mapping system for different DCAT variants
- **Codelists**: Spanish and EU vocabulary mappings in `ckanext/dcat/codelists/`

### Profile System

Profiles define bidirectional mapping between RDF and CKAN:
- `parse_dataset()`: RDF → CKAN dataset dict
- `graph_from_dataset()`: CKAN dataset → RDF graph
- `parse_catalog()` / `graph_from_catalog()`: Catalog-level mappings

Default profile supports DCAT-AP v2.1 with backward compatibility for v1.1.

### Configuration

Key config options:
- `ckanext.dcat.rdf.profiles`: Active profiles (default: `euro_dcat_ap_2`)
- `ckanext.dcat.enable_rdf_endpoints`: Enable/disable RDF endpoints
- `ckanext.dcat.enable_content_negotiation`: Content negotiation support
- `ckanext.dcat.catalog_endpoint`: Custom catalog endpoint pattern
- `ckanext.dcat.compatibility_mode`: Legacy field compatibility

### Extension Points

- Implement `IDCATRDFHarvester` interface for custom harvester behavior
- Create custom profiles extending `RDFProfile` for specialized mappings
- Override templates in `ckanext/dcat/templates/` for UI customization

This is a mature extension focused on semantic interoperability, designed for custom CKAN deployments requiring DCAT compliance and European data portal integration.