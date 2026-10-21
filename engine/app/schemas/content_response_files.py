from typing import Literal, NotRequired

from typing_extensions import TypedDict

type FilePreviewKind = Literal["markdown", "html", "text", "csv", "pdf", "xlsx", "docx", "pptx", "image", "binary"]


class FilePreviewBase(TypedDict):
    path: str
    mime_type: str
    download_url: str


class TextFilePreview(FilePreviewBase):
    kind: Literal["markdown", "html", "text"]
    content: str
    content_hash: str


class CsvFilePreview(FilePreviewBase):
    kind: Literal["csv"]
    content: str
    content_hash: str
    rows: list[list[str]]


class PdfFilePreview(FilePreviewBase):
    kind: Literal["pdf"]
    url: str


class SpreadsheetFilePreview(FilePreviewBase):
    kind: Literal["xlsx"]
    text: str
    sheets: NotRequired[list[dict[str, object]]]
    companion_path: NotRequired[str]


class OfficeFilePreview(FilePreviewBase):
    kind: Literal["docx", "pptx"]
    text: str
    companion_path: str | None


class BinaryFilePreview(FilePreviewBase):
    kind: Literal["image", "binary"]
    size: int
    base64_sample: str


type FilePreview = (
    TextFilePreview | CsvFilePreview | PdfFilePreview | SpreadsheetFilePreview | OfficeFilePreview | BinaryFilePreview
)


class FilePathAcknowledgement(TypedDict):
    status: Literal["ok"]
    path: str


class FileWriteAcknowledgement(FilePathAcknowledgement):
    revision_id: str | None


class FileLockAcknowledgement(FilePathAcknowledgement):
    expires_at: str


class FileRevisionResponse(TypedDict):
    id: str
    path: str
    operation: str
    actor_type: str
    actor_id: str | None
    session_id: str | None
    before_content: str | None
    after_content: str | None
    created_at: str | None
    updated_at: str | None


class AgentSkillImportAcknowledgement(TypedDict):
    status: Literal["ok"]
    folder_name: str
    files_written: int
    files: list[str]


class NamedAgentSkillImportAcknowledgement(AgentSkillImportAcknowledgement):
    skill_name: str


class FileUploadResponse(FilePathAcknowledgement):
    url: str
    filename: str
    size: int
    extracted_text_path: str | None


class EnterpriseFileEntry(TypedDict):
    name: str
    path: str
    is_dir: bool
    size: int
    url: str | None


class EnterpriseFileContent(TypedDict):
    path: str
    content: str


class ChatUploadResponse(TypedDict):
    filename: str
    saved_filename: str
    size: int
    extracted_text: str
    workspace_path: str
    is_image: bool
    image_data_url: str
