# Project documentation

This folder documents the code and data as handled by this repository. The
root README contains instructions for using the application; these pages
explain its implementation and files in more detail.

- [Scripts](scripts.md): what each script does and how they work together.
- [API JSON](api-json.md): the structure of `scp-data.tedivm.com` and the
  fields used by the loader.
- [`scp_data/`](scp-data.md): the data we download and the derived files
  generated locally by the application.

API data and the contents of `scp_data/` change with updates. Examples
describe the observed structure and how the code uses it; they do not
guarantee that every field will always have the same value.
