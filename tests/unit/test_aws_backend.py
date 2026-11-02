"""S3 backend tests via moto, driven through the public API."""

from pathlib import Path

import numpy as np
import pytest
from botocore.exceptions import (
    BotoCoreError, ConnectionClosedError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError,
)

import ondio
from ondio import ObjectExistsError, ObjectNotFoundError, OndioError
from ondio.backends import AwsBackend
from ondio.registry import get_backend

ENDPOINT = "http://127.0.0.1:9000"


@pytest.fixture(autouse=True)
def prefix(aws_bucket):
    """The moto bucket's s3:// prefix (autouse: every test runs inside moto)."""
    return aws_bucket


class TestReadWrite:
    def test_write_read_roundtrip(self, prefix):
        ondio.write(f"{prefix}/a/b.bin", b"payload")
        assert ondio.read(f"{prefix}/a/b.bin") == b"payload"

    def test_read_missing_raises(self, prefix):
        with pytest.raises(ObjectNotFoundError):
            ondio.read(f"{prefix}/absent")

    def test_download(self, prefix, tmp_path):
        uri = f"{prefix}/a.bin"
        ondio.write(uri, b"data")
        dest = tmp_path / "nested" / "a.bin"
        ondio.download(uri, dest)
        assert dest.read_bytes() == b"data"


class TestUpload:
    def test_upload_larger_than_one_multipart_chunk(self, prefix, tmp_path, monkeypatch):
        # moto stores a multipart object's composite CRC32 without the "-N"
        # suffix real S3 adds, so botocore would check it against the whole
        # body and fail the read
        monkeypatch.setenv("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
        # 9 MiB is above boto3's 8 MiB multipart threshold
        payload = np.random.default_rng(11).bytes(9 * 1024 * 1024)
        src = tmp_path / "big.bin"
        src.write_bytes(payload)
        uri = f"{prefix}/big.bin"
        ondio.upload(uri, src)
        assert ondio.read(uri) == payload

    def test_upload_does_not_read_whole_source_or_write(self, prefix, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")

        def refuse(*args, **kwargs):
            raise AssertionError("upload went through an in-memory read or write")

        monkeypatch.setattr(Path, "read_bytes", refuse)
        monkeypatch.setattr("ondio.backends.aws.AwsBackend.write", refuse)
        uri = f"{prefix}/src.bin"
        ondio.upload(uri, src)
        assert ondio.read(uri) == b"contents"

    def test_upload_replaces_existing_object(self, prefix, tmp_path):
        first, second = tmp_path / "first.bin", tmp_path / "second.bin"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        uri = f"{prefix}/a.bin"
        ondio.upload(uri, first)
        ondio.upload(uri, second)
        assert ondio.read(uri) == b"second"

    def test_upload_to_missing_bucket_raises(self, tmp_path):
        src = tmp_path / "src.bin"
        src.write_bytes(b"contents")
        with pytest.raises(ObjectNotFoundError) as excinfo:
            ondio.upload("s3://absent-bucket/a.bin", src)
        assert excinfo.value.__cause__ is not None

    def test_upload_missing_source_raises(self, prefix, tmp_path):
        with pytest.raises(FileNotFoundError):
            ondio.upload(f"{prefix}/a.bin", tmp_path / "absent.bin")

class TestReadRange:
    def test_read_range_semantics(self, prefix):
        uri = f"{prefix}/digits.bin"
        ondio.write(uri, b"0123456789")
        backend = get_backend(uri)
        assert backend.read_range(uri, 2, 5) == b"2345"
        assert backend.read_range(uri, 5, None) == b"56789"
        assert backend.read_range(uri, 8, 100) == b"89"  # end clamped by S3
        assert backend.read_range(uri, 50, None) == b""  # InvalidRange -> empty
        assert backend.size(uri) == 10

    def test_read_range_invalid_args(self, prefix):
        uri = f"{prefix}/digits.bin"
        ondio.write(uri, b"0123456789")
        backend = get_backend(uri)
        with pytest.raises(ValueError):
            backend.read_range(uri, -1, 5)
        with pytest.raises(ValueError):
            backend.read_range(uri, 5, 2)


class TestExistsDelete:
    def test_exists_and_idempotent_delete(self, prefix):
        uri = f"{prefix}/a.bin"
        ondio.write(uri, b"x")
        assert ondio.exists(uri)
        ondio.delete(uri)
        ondio.delete(uri)  # idempotent, no error
        assert not ondio.exists(uri)


class TestListing:
    @pytest.fixture
    def tree(self, prefix):
        for key, content in {
            "audio/chunk_001.flac": b"one",
            "audio/chunk_002.flac": b"two",
            "audio/notes.txt": b"text",
            "audio/sub/chunk_003.flac": b"three",
        }.items():
            ondio.write(f"{prefix}/{key}", content)

    def test_list_files(self, prefix, tree):
        assert ondio.list_files(f"{prefix}/audio/") == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/notes.txt",
            f"{prefix}/audio/sub/chunk_003.flac",
        ]
        assert ondio.list_files(f"{prefix}/audio/", recursive=False) == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/notes.txt",
        ]
        assert len(ondio.list_files(f"{prefix}/audio/", max_items=2)) == 2

    def test_folder_semantics_without_trailing_slash(self, prefix, tree):
        # no trailing slash lists the folder's contents, recursive or not
        assert ondio.list_files(f"{prefix}/audio") == ondio.list_files(f"{prefix}/audio/")
        assert ondio.list_files(f"{prefix}/audio", recursive=False) == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/notes.txt",
        ]

    def test_partial_name_path_never_matches(self, prefix, tree):
        # folder semantics: "chunk_" is neither a folder nor an exact key
        assert ondio.list_files(f"{prefix}/audio/chunk_") == []
        assert ondio.object_count(f"{prefix}/audio/chunk_") == 0

    def test_listing_a_key_yields_that_key(self, prefix, tree):
        uri = f"{prefix}/audio/chunk_001.flac"
        assert ondio.list_files(uri) == [uri]
        assert ondio.list_files(uri, recursive=False) == [uri]
        assert ondio.object_count(uri) == 1
        assert ondio.list_files(uri, required_ext="flac") == [uri]
        assert ondio.list_files(uri, required_ext="txt") == []

    def test_trailing_slash_excludes_the_exact_key(self, prefix, tree):
        # a trailing slash names folder contents only
        uri = f"{prefix}/audio/chunk_001.flac"
        assert ondio.list_files(f"{uri}/") == []
        assert ondio.object_count(f"{uri}/") == 0

    def test_max_items_counts_the_exact_key(self, prefix, tree):
        # the exact key sorts ahead of everything under it, so the server-side
        # cap stays valid — this breaks if the cap is applied without it
        ondio.write(f"{prefix}/audio", b"an object at the folder's own key")
        assert ondio.list_files(f"{prefix}/audio", max_items=2) == [
            f"{prefix}/audio",
            f"{prefix}/audio/chunk_001.flac",
        ]

    def test_sibling_prefixes_are_isolated(self, prefix):
        for key in ["results/run-1/a.parquet", "results/run-10/b.parquet", "results/run-111/c.parquet"]:
            ondio.write(f"{prefix}/{key}", b"x")
        assert ondio.list_files(f"{prefix}/results/run-1") == [
            f"{prefix}/results/run-1/a.parquet"
        ]
        assert ondio.object_count(f"{prefix}/results/run-1") == 1

    def test_list_files_filename_filters(self, prefix, tree):
        flacs = ondio.list_files(f"{prefix}/audio/", required_ext="flac")
        assert flacs == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
            f"{prefix}/audio/sub/chunk_003.flac",  # nested key matched by filename
        ]
        assert ondio.list_files(f"{prefix}/audio/", required_ext=".flac") == flacs
        assert ondio.list_files(f"{prefix}/audio/", required_prefix="chunk_") == flacs
        assert ondio.list_files(f"{prefix}/audio/", required_prefix="chunk_", required_ext="txt") == []

    def test_list_files_max_items_applies_after_filters(self, prefix, tree):
        # keys that sort before the flacs: a native MaxItems cap would fill the
        # page with these and starve the filtered result
        for i in (1, 2, 3):
            ondio.write(f"{prefix}/audio/a{i}.txt", b"")
        assert ondio.list_files(f"{prefix}/audio/", required_ext="flac", max_items=2) == [
            f"{prefix}/audio/chunk_001.flac",
            f"{prefix}/audio/chunk_002.flac",
        ]

    def test_list_files_skips_folder_placeholders(self, prefix, tree):
        ondio.write(f"{prefix}/audio/sub2/", b"")  # console-style folder marker
        listed = ondio.list_files(f"{prefix}/audio/")
        assert f"{prefix}/audio/sub2/" not in listed
        assert len(listed) == 4

    def test_object_count_skips_folder_placeholders(self, prefix, tree):
        ondio.write(f"{prefix}/audio/sub2/", b"")  # console-style folder marker
        assert ondio.object_count(f"{prefix}/audio/") == 4
        # count and listing always agree, placeholders or not
        assert ondio.object_count(f"{prefix}/audio/") == len(ondio.list_files(f"{prefix}/audio/"))

    def test_object_count(self, prefix, tree):
        assert ondio.object_count(f"{prefix}/audio/") == 4
        assert ondio.object_count(f"{prefix}/audio/", required_ext="flac") == 3
        assert ondio.object_count(f"{prefix}/audio/", required_prefix="chunk_") == 3
        assert ondio.object_count(f"{prefix}/audio/", required_prefix="chunk_", required_ext=".txt") == 0


class TestJson:
    def test_json_roundtrip(self, prefix):
        uri = f"{prefix}/meta.json"
        data = {"id": "rec-7", "tags": ["dawn"]}
        ondio.write_json(uri, data)
        assert ondio.read_json(uri) == data


class TestCreate:
    def test_create_writes_a_new_object(self, prefix):
        ondio.create(f"{prefix}/runs/1/result.json", b"first")
        assert ondio.read(f"{prefix}/runs/1/result.json") == b"first"

    def test_create_refuses_an_existing_object_and_keeps_it(self, prefix):
        uri = f"{prefix}/result.json"
        ondio.create(uri, b"first")
        with pytest.raises(ObjectExistsError):
            ondio.create(uri, b"second")
        assert ondio.read(uri) == b"first"

    def test_a_conflicting_write_is_not_reported_as_existing(self, prefix, monkeypatch):
        # S3 answers 409 when another conditional write to the key is in progress.
        # That does not prove an object exists, so it is a plain OndioError.
        from botocore.exceptions import ClientError

        uri = f"{prefix}/result.json"
        backend = get_backend(uri)

        def conflict(**kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "ConditionalRequestConflict"},
                    "ResponseMetadata": {"HTTPStatusCode": 409},
                },
                "PutObject",
            )

        monkeypatch.setattr(backend._client, "put_object", conflict)
        with pytest.raises(OndioError) as raised:
            backend.create(uri, b"x")
        assert not isinstance(raised.value, ObjectExistsError)

    @pytest.mark.parametrize("error_name", ["ReadTimeoutError", "EndpointConnectionError"])
    def test_transport_failure_is_an_uncertain_write(self, prefix, monkeypatch, error_name):
        from botocore import exceptions
        import ondio.dispatcher as dispatcher

        uri = f"{prefix}/result.json"
        backend = get_backend(uri)
        failure = getattr(exceptions, error_name)(endpoint_url="https://s3.example.invalid")

        def fail(**kwargs):
            raise failure

        monkeypatch.setattr(backend._client, "put_object", fail)
        monkeypatch.setattr(dispatcher, "get_backend", lambda uri, **kwargs: backend)
        with pytest.raises(OndioError) as raised:
            ondio.create(uri, b"x")
        assert type(raised.value) is ondio.NetworkError
        assert "could not confirm create" in str(raised.value)
        assert raised.value.__cause__ is failure


def test_an_s3_location_is_the_key_ondio_writes(prefix):
    boto3 = pytest.importorskip("boto3")
    uri = f"{prefix}/runs/%2e%2e/a.bin"
    ondio.write(uri, b"payload")
    store, key = ondio.locate(uri)
    client = boto3.client("s3", region_name="us-east-1")
    body = client.get_object(Bucket=store.removeprefix("s3://"), Key=key)["Body"].read()
    assert body == b"payload"


class TestRefusedUris:
    @pytest.fixture(params=["?b", "#b", "?", "#"])
    def uri(self, prefix, request):
        return f"{prefix}/a{request.param}"

    def test_read_refuses_a_query_or_fragment(self, prefix, uri):
        ondio.write(f"{prefix}/a", b"payload")
        with pytest.raises(ValueError):
            ondio.read(uri)

    @pytest.mark.parametrize("operation", [ondio.create, ondio.write])
    def test_writes_refuse_a_query_or_fragment(self, prefix, uri, operation):
        with pytest.raises(ValueError):
            operation(uri, b"payload")
        assert ondio.list_files(f"{prefix}/") == []

    def test_an_encoded_question_mark_is_part_of_the_key(self, prefix):
        ondio.write(f"{prefix}/a%3Fb", b"payload")
        assert ondio.list_files(f"{prefix}/") == [f"{prefix}/a%3Fb"]


def test_endpoint_url_reaches_the_s3_client():
    backend = get_backend("s3://bucket/key", endpoint_url=ENDPOINT)
    assert backend._client.meta.endpoint_url == ENDPOINT


def test_endpoint_url_and_session_options_are_both_used():
    backend = get_backend("s3://bucket/key", endpoint_url=ENDPOINT, region_name="eu-west-1")
    assert backend._client.meta.endpoint_url == ENDPOINT
    assert backend._client.meta.region_name == "eu-west-1"


def test_the_environment_gives_a_new_client_the_same_endpoint(monkeypatch):
    for name, value in ondio.get_environment("s3://bucket/key", endpoint_url=ENDPOINT).items():
        monkeypatch.setenv(name, value)
    assert get_backend("s3://bucket/key")._client.meta.endpoint_url == ENDPOINT


def _raise(exc):
    def fail(*args, **kwargs):
        raise exc
    return fail


class TestErrorTranslation:
    def test_unknown_profile_is_an_auth_error(self):
        with pytest.raises(ondio.AuthError, match="for the new S3 client: ProfileNotFound"):
            AwsBackend(profile_name="nope")

    def test_bad_option_name_stays_a_type_error(self):
        with pytest.raises(TypeError):
            AwsBackend(bogus_option=1)

    def test_bad_region_stays_a_value_error(self):
        # botocore's InvalidRegionError is both a BotoCoreError and a ValueError.
        with pytest.raises(ValueError) as info:
            AwsBackend(region_name="bad region!")
        assert not isinstance(info.value, ondio.OndioError)

    @pytest.mark.parametrize("method", ["get_object", "head_object", "put_object"])
    def test_missing_credentials_are_an_auth_error(self, prefix, monkeypatch, method):
        backend = get_backend(f"{prefix}/k")
        monkeypatch.setattr(backend._client, method, _raise(NoCredentialsError()))
        call = {"get_object": lambda: backend.read_range(f"{prefix}/k", 0, 9),
                "head_object": lambda: backend.size(f"{prefix}/k"),
                "put_object": lambda: backend.create(f"{prefix}/k", b"x")}[method]
        with pytest.raises(ondio.AuthError):
            call()

    @pytest.mark.parametrize("exc", [
        EndpointConnectionError(endpoint_url="http://127.0.0.1:9"),
        ReadTimeoutError(endpoint_url="http://127.0.0.1:9"),
        ConnectionClosedError(endpoint_url="http://127.0.0.1:9"),
    ])
    def test_transport_failures_are_network_errors(self, prefix, monkeypatch, exc):
        backend = get_backend(f"{prefix}/k")
        monkeypatch.setattr(backend._client, "get_object", _raise(exc))
        with pytest.raises(ondio.NetworkError) as info:
            backend.read_range(f"{prefix}/k", 0, 9)
        assert info.value.__cause__ is exc
        with pytest.raises(ondio.NetworkError):
            backend.read(f"{prefix}/k")

    def test_other_botocore_error_is_an_ondio_error(self, prefix, monkeypatch):
        backend = get_backend(f"{prefix}/k")
        monkeypatch.setattr(backend._client, "head_object", _raise(BotoCoreError()))
        with pytest.raises(OndioError) as info:
            backend.size(f"{prefix}/k")
        assert not isinstance(info.value, (ondio.AuthError, ondio.NetworkError))

    def test_create_transport_failure_cannot_confirm(self, prefix, monkeypatch):
        backend = get_backend(f"{prefix}/k")
        monkeypatch.setattr(backend._client, "put_object",
                            _raise(ReadTimeoutError(endpoint_url="http://x")))
        with pytest.raises(ondio.NetworkError, match="could not confirm create"):
            backend.create(f"{prefix}/k", b"x")

class TestGivenClient:
    def test_a_given_client_is_used(self, prefix):
        boto3 = pytest.importorskip("boto3")
        client = boto3.client("s3", region_name="us-east-1")
        ondio.write(f"{prefix}/k", b"abc")
        backend = AwsBackend(client=client)
        assert backend._client is client
        assert backend.read(f"{prefix}/k") == b"abc"

    @pytest.mark.parametrize("option", [{"endpoint_url": "http://x"}, {"region_name": "eu-west-1"}])
    def test_client_with_another_option_is_refused(self, option):
        boto3 = pytest.importorskip("boto3")
        with pytest.raises(ValueError) as raised:
            AwsBackend(client=boto3.client("s3", region_name="us-east-1"), **option)
        assert str(next(iter(option.values()))) not in str(raised.value)

class TestTransferErrors:
    def test_download_retries_exhausted_is_a_network_error(self, prefix, monkeypatch, tmp_path):
        from boto3.exceptions import RetriesExceededError

        backend = get_backend(f"{prefix}/k")
        failure = RetriesExceededError(ReadTimeoutError(endpoint_url="http://x"))
        monkeypatch.setattr(backend._client, "download_file", _raise(failure))
        with pytest.raises(ondio.NetworkError) as raised:
            backend.download(f"{prefix}/k", tmp_path / "k")
        assert raised.value.__cause__ is failure

    def test_download_transfer_failure_is_an_ondio_error(self, prefix, monkeypatch, tmp_path):
        from boto3.exceptions import S3TransferFailedError

        backend = get_backend(f"{prefix}/k")
        monkeypatch.setattr(backend._client, "download_file", _raise(S3TransferFailedError("x")))
        with pytest.raises(OndioError):
            backend.download(f"{prefix}/k", tmp_path / "k")


def test_credential_process_output_is_not_in_the_message(prefix, monkeypatch):
    from botocore.exceptions import CredentialRetrievalError

    backend = get_backend(f"{prefix}/k")
    failure = CredentialRetrievalError(provider="custom-process", error_msg="SECRET-OUTPUT")
    monkeypatch.setattr(backend._client, "head_object", _raise(failure))
    with pytest.raises(ondio.AuthError) as raised:
        backend.size(f"{prefix}/k")
    assert "SECRET-OUTPUT" not in str(raised.value)


@pytest.mark.parametrize("call", [
    lambda b, uri: b.read(uri),
    lambda b, uri: b.size(uri),
    lambda b, uri: b.create(uri, b"x"),
])
def test_an_empty_key_is_a_value_error(prefix, call):
    # botocore refuses an empty key with ParamValidationError, which is a BotoCoreError.
    backend = get_backend(f"{prefix}/")
    with pytest.raises(ValueError) as raised:
        call(backend, f"{prefix}/")
    assert not isinstance(raised.value, OndioError)


def test_a_body_cut_short_is_a_network_error(prefix, monkeypatch):
    from botocore.exceptions import IncompleteReadError

    class Body:
        def read(self):
            raise IncompleteReadError(actual_bytes=1, expected_bytes=10)

    backend = get_backend(f"{prefix}/k")
    monkeypatch.setattr(backend._client, "get_object", lambda **kwargs: {"Body": Body()})
    with pytest.raises(ondio.NetworkError):
        backend.read(f"{prefix}/k")


class TestAnonymous:
    def test_anonymous_client_is_unsigned(self):
        from botocore import UNSIGNED

        backend = AwsBackend(anonymous=True, region_name="eu-west-1", endpoint_url=ENDPOINT)
        assert backend._client.meta.config.signature_version is UNSIGNED
        assert backend._client.meta.region_name == "eu-west-1"
        assert backend._client.meta.endpoint_url == ENDPOINT

    def test_anonymous_through_the_dispatcher_reads_a_public_object(self, prefix):
        AwsBackend()._client.put_object(Bucket=prefix.removeprefix("s3://"), Key="pub.bin", Body=b"open", ACL="public-read")
        assert ondio.read(f"{prefix}/pub.bin", anonymous=True) == b"open"

    @pytest.mark.parametrize("variable", ["AWS_PROFILE", "AWS_DEFAULT_PROFILE"])
    def test_anonymous_ignores_a_profile_from_the_environment(self, prefix, monkeypatch, variable):
        AwsBackend()._client.put_object(Bucket=prefix.removeprefix("s3://"), Key="pub.bin", Body=b"open", ACL="public-read")
        monkeypatch.setenv(variable, "does-not-exist")
        assert ondio.read(f"{prefix}/pub.bin", anonymous=True) == b"open"

    def test_anonymous_read_of_a_private_object_is_an_auth_error(self, prefix):
        ondio.write(f"{prefix}/private.bin", b"closed")
        with pytest.raises(ondio.AuthError):
            ondio.read(f"{prefix}/private.bin", anonymous=True)

    @pytest.mark.parametrize("option", [
        {"profile_name": "secret-profile"}, {"aws_access_key_id": "secret-id"}, {"aws_session_token": "t0ken"},
    ])
    def test_anonymous_refuses_credential_options_by_name(self, option):
        with pytest.raises(ValueError) as info:
            AwsBackend(anonymous=True, **option)
        name, value = next(iter(option.items()))
        assert name in str(info.value) and value not in str(info.value)

    def test_anonymous_with_client_is_refused(self):
        with pytest.raises(ValueError, match="anonymous"):
            AwsBackend(client=object(), anonymous=True)


class TestUploadWithoutOverwrite:
    def test_upload_without_overwrite_writes_a_new_object(self, tmp_path, prefix):
        source = tmp_path / "src.bin"
        source.write_bytes(b"first")
        ondio.upload(f"{prefix}/new.bin", source, overwrite=False)
        assert ondio.read(f"{prefix}/new.bin") == b"first"

    def test_upload_without_overwrite_keeps_an_existing_object(self, tmp_path, prefix):
        uri = f"{prefix}/old.bin"
        ondio.write(uri, b"first")
        source = tmp_path / "src.bin"
        source.write_bytes(b"second")
        with pytest.raises(ObjectExistsError):
            ondio.upload(uri, source, overwrite=False)
        assert ondio.read(uri) == b"first"

    def test_upload_without_overwrite_refuses_a_file_over_the_put_limit(self, tmp_path, prefix, monkeypatch):
        monkeypatch.setattr("ondio.backends.aws._MAX_SINGLE_PUT", 4)
        source = tmp_path / "big.bin"
        source.write_bytes(b"12345")
        with pytest.raises(ondio.UnsupportedOperationError, match="4"):
            ondio.upload(f"{prefix}/big.bin", source, overwrite=False)
        assert not ondio.exists(f"{prefix}/big.bin")

    def test_upload_without_overwrite_sends_one_conditional_put(self, tmp_path, prefix, monkeypatch):
        backend = get_backend(f"{prefix}/k")
        sent = []
        real = backend._client.put_object
        monkeypatch.setattr(backend._client, "put_object", lambda **kw: sent.append(kw) or real(**kw))
        monkeypatch.setattr(backend._client, "upload_file", _raise(AssertionError("upload_file")))
        source = tmp_path / "src.bin"
        source.write_bytes(b"x")
        ondio.upload(f"{prefix}/k", source, overwrite=False, backend=backend)
        assert len(sent) == 1
        assert sent[0]["IfNoneMatch"] == "*" and not isinstance(sent[0]["Body"], bytes)
