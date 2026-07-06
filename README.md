# ondio - 

This is a repository for `ondio` --- _ondas_ (Spanish for waves) + io --- a Python package
for managing IO of audio data (specifically just `.flac` data at the moment) and results
derived from bioacoustic models (specfically as `.json` and `.parquet` files) from a variety
of sources (AWS S3, GCS, HTTP/HTTPS, local filesystems).

## Architecture 

`ondio` defines a unified interface for reading/writing files across different platforms, where the target platform is inferred based on the structure of the URI.

The URI structure is used to dispatch to platform specific implementations, combining [strategy](https://en.wikipedia.org/wiki/Strategy_pattern) and [registry] design patterns.


