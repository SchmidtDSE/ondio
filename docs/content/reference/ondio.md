# `ondio`

Everything here is importable from `ondio` itself, for example `ondio.read` or
`ondio.OndioError`.

::: ondio
    options:
      show_root_heading: false
      show_submodules: false
      members:
        - read
        - read_range
        - write
        - create
        - read_json
        - write_json
        - download
        - download_files
        - upload
        - upload_files
        - exists
        - delete
        - list_files
        - object_count
        - locate
        - detect_platform
        - get_backend
        - get_environment
        - extract_flac_header
        - read_flac
        - download_flac
        - set_printer
        - FlacHeader
        - Location
        - OndioError
        - ObjectNotFoundError
        - ObjectExistsError
        - AuthError
        - NetworkError
        - UnsupportedOperationError
        - UnknownPlatformError
