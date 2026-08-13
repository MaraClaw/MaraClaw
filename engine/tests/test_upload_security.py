import pytest

from app.api.upload import validate_upload_filename


@pytest.mark.parametrize("filename", ["../secret.pdf", "nested/file.pdf", "nested\\file.pdf", ".", ".."])
def test_validate_upload_filename_rejects_path_components(filename: str):
    # Given: an upload name containing a path component
    # When: the route validates the upload boundary
    # Then: it rejects a name that could escape its storage location
    with pytest.raises(ValueError, match="filename"):
        validate_upload_filename(filename)


def test_validate_upload_filename_accepts_a_plain_filename():
    # Given: a normal client filename
    # When: the route validates the upload boundary
    filename = validate_upload_filename("report.pdf")

    # Then: the storage name is preserved
    assert filename == "report.pdf"
