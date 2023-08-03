import io
import json
import logging
import mimetypes
import os
import re
import tempfile
import traceback
from collections import OrderedDict
from functools import partial
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Set,
    Union,
    cast,
)
from uuid import UUID, uuid4

from pydantic import root_validator
from pydantic.class_validators import validator
from pydantic.config import Extra
from pydantic.fields import Field
from pydantic.main import BaseModel
from pydantic.types import FilePath

from langchain.document_loaders.csv_loader import CSVLoader
from langchain.document_loaders.notebook import NotebookLoader
from langchain.document_loaders.text import TextLoader
from langchain.load.serializable import Serializable
from langchain.prompts import PromptTemplate
from langchain.schema import Document

logger = logging.getLogger(__name__)

FORMAT_INSTRUCTION = (
    "The input should be formatted as a list of entities separated"
    " with a space. As an example, a list of keywords is 'hello word'."
)


_acceptable_params_of_list = {
    "corpora",
    "driveId",
    "fields",
    "includeItemsFromAllDrives",
    "orderBy",
    "pageSize",
    "pageToken",
    "q",
    "spaces",
    "supportsAllDrives",
    "includePermissionsForView",
    "includeLabels",
}


# Manage:
# - File in trash
# - Shortcut
# - Paging with request GDrive list()
# - Multiple kind of template for request GDrive
# - Convert a lot of mime type (can be configured)
# - Convert GDoc, GSheet and GSlide
# - Can use only the description of files, without conversion of the body
# - Lambda filter
# - Remove duplicate document (via shortcut)
# - All GDrive api parameters
# - Url to documents
# - Environment variable for reference the API tokens
# - Different kind of strange state with Google File (absence of URL, etc.)
SCOPES: List[str] = [
    # See https://developers.google.com/identity/protocols/oauth2/scopes
    "https://www.googleapis.com/auth/drive.readonly",
]


class _LRUCache:
    # initialising capacity
    def __init__(self, capacity: int = 30):
        self._cache: OrderedDict = OrderedDict()
        self._capacity: int = capacity

    def get(self, key: str) -> Optional[str]:
        if key not in self._cache:
            return None
        else:
            self._cache.move_to_end(key)
            return self._cache[key]

    def put(self, key: str, value: str) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        if len(self._cache) > self._capacity:
            self._cache.popitem(last=False)


def default_conv_loader(
    mode: Literal["single", "elements"] = "single",
    strategy: Literal["strategy", "fast"] = "fast",
    ocr_languages: str = "eng",
) -> dict:
    mime_types_mapping = {
        "text/text": TextLoader,
        "text/plain": TextLoader,
        "text/csv": CSVLoader,
        "application/vnd.google.colaboratory": partial(
            lambda file_path: NotebookLoader(
                path=file_path, include_outputs=False, remove_newline=True
            )
        ),
    }
    try:
        from langchain.document_loaders import UnstructuredRTFLoader

        mime_types_mapping["application/rtf"] = UnstructuredRTFLoader
    except ImportError:
        logger.info("Ignore RTF for GDrive (use `pip install pypandoc_binary`)")
    try:
        import unstructured  # noqa: F401

        from langchain.document_loaders import (
            UnstructuredEPubLoader,
            UnstructuredFileLoader,
            UnstructuredHTMLLoader,
            UnstructuredImageLoader,
            UnstructuredMarkdownLoader,
            UnstructuredODTLoader,
            UnstructuredPDFLoader,
            UnstructuredPowerPointLoader,
            UnstructuredWordDocumentLoader,
        )

        mime_types_mapping.update(
            {
                "image/png": partial(
                    UnstructuredImageLoader, ocr_languages=ocr_languages
                ),
                "image/jpeg": partial(
                    UnstructuredImageLoader, ocr_languages=ocr_languages
                ),
                "application/json": partial(
                    UnstructuredFileLoader, ocr_languages=ocr_languages
                ),
            }
        )
        mime_types_mapping.update(
            {
                "application/epub+zip": UnstructuredEPubLoader,
            }
        )
        mime_types_mapping.update(
            {
                "application/pdf": partial(
                    UnstructuredPDFLoader, strategy=strategy, mode=mode
                ),
            }
        )
        mime_types_mapping.update(
            {
                "text/html": UnstructuredHTMLLoader,
                "text/markdown": UnstructuredMarkdownLoader,
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation": partial(
                    UnstructuredPowerPointLoader, mode=mode
                ),  # PPTX
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document": partial(
                    UnstructuredWordDocumentLoader, mode=mode
                ),  # DOCX
                "application/vnd.oasis.opendocument.text": UnstructuredODTLoader,
            }
        )
    except ImportError:
        logger.info(
            "Ignore Unstructured*Loader for GDrive "
            "(no module `unstructured[local-inference]`)"
        )

    return mime_types_mapping


def get_template(type: str) -> PromptTemplate:
    return {
        "gdrive-all-in-folder": PromptTemplate(
            input_variables=["folder_id"],
            template=" '{folder_id}' in parents and trashed=false",
        ),
        "gdrive-query": PromptTemplate(
            input_variables=["query"],
            template="fullText contains '{query}' and trashed=false",
        ),
        "gdrive-by-name": PromptTemplate(
            input_variables=["query"],
            template="name contains '{query}' and trashed=false",
        ),
        "gdrive-by-name-in-folder": PromptTemplate(
            input_variables=["query", "folder_id"],
            template="name contains '{query}' "
            "and '{folder_id}' in parents "
            "and trashed=false",
        ),
        "gdrive-query-in-folder": PromptTemplate(
            input_variables=["query", "folder_id"],
            template="fullText contains '{query}' "
            "and '{folder_id}' in parents "
            "and trashed=false",
        ),
        "gdrive-mime-type": PromptTemplate(
            input_variables=["mime_type"],
            template="mimeType = '{mime_type}' and trashed=false",
        ),
        "gdrive-mime-type-in-folder": PromptTemplate(
            input_variables=["mime_type", "folder_id"],
            template="mimeType = '{mime_type}' "
            "and '{folder_id}' in parents "
            "and trashed=false",
        ),
        "gdrive-query-with-mime-type": PromptTemplate(
            input_variables=["query", "mime_type"],
            template="(fullText contains '{query}' "
            "and mime_type = '{mime_type}') "
            "and trashed=false",
        ),
        "gdrive-query-with-mime-type-and-folder": PromptTemplate(
            input_variables=["query", "mime_type", "folder_id"],
            template="((fullText contains '{query}') and mime_type = '{mime_type}')"
            "and '{folder_id}' in parents "
            "and trashed=false",
        ),
    }[type]


def _snippet_from_page_content(page_content: str, max_size: int = 50) -> str:
    if max_size < 6:
        raise ValueError("max_size must be >=6")
    part_size = max_size // 2
    strip_content = re.sub(r"(\s|<PAGE BREAK>)+", r" ", page_content).strip()
    if len(strip_content) <= max_size:
        return strip_content
    elif len(strip_content) <= max_size + 3:
        return (strip_content[:part_size] + "...")[:max_size]
    return strip_content[:part_size] + "..." + strip_content[-part_size:]


def _extract_mime_type(file: Dict[str, Any]) -> str:
    """Extract mime type or try to deduce from the filename and webViewLink"""
    if "mimeType" in file:
        mime_type = file["mimeType"]
    else:
        # Try to deduce the mime_type
        if "shortcutDetails" in file:
            return "application/vnd.google-apps.shortcut"

        suffix = Path(file["name"]).suffix
        mime_type = mimetypes.types_map.get(suffix)
        if not mime_type:
            if "webViewLink" in file:
                match = re.search(
                    r"drive\.google\.com/drive/(.*)/", file["webViewLink"]
                )
                if match:
                    mime_type = "application/vnd.google-apps." + match.groups()[0]
                else:
                    mime_type = "unknown"
            else:
                mime_type = "unknown"
            logger.debug(f"Calculate mime_type='{mime_type}' for file '{file['name']}'")
    return mime_type


class GoogleDriveUtilities(Serializable, BaseModel):
    """
    Loader that loads documents from Google Drive.

    All files that can be converted to text can be converted to `Document`.
    - All documents use the `conv_mapping` to extract the text.

    At this time, the default list of accepted mime-type is:
    - text/text
    - text/plain
    - text/html
    - text/csv
    - text/markdown
    - image/png
    - image/jpeg
    - application/epub+zip
    - application/pdf
    - application/rtf
    - application/vnd.google-apps.document (GDoc)
    - application/vnd.google-apps.presentation (GSlide)
    - application/vnd.google-apps.spreadsheet (GSheet)
    - application/vnd.google.colaboratory (Notebook colab)
    - application/vnd.openxmlformats-officedocument.presentationml.presentation (PPTX)
    - application/vnd.openxmlformats-officedocument.wordprocessingml.document (DOCX)

    All empty files are ignored.

    The code use the Google API v3. To have more information about some parameters,
    see [here](https://developers.google.com/drive/api/v3/reference/files/list).

    The application must be authenticated with a json file.
    The format may be for a user or for an application via a service account.
    The environment variable `GOOGLE_ACCOUNT_FILE` may be set to reference this file.
    For more information, see [here]
    (https://developers.google.com/workspace/guides/auth-overview).

    All parameter compatible with Google [`list()`]
    (https://developers.google.com/drive/api/v3/reference/files/list)
    API can be set.

    To specify the new pattern of the Google request, you can use a `PromptTemplate()`.
    The variables for the prompt can be set with `kwargs` in the constructor.
    Some pre-formated request are proposed (use {query}, {folder_id}
    and/or {mime_type}):
    - "gdrive-all-in-folder":                   Return all compatible files from a
                                                 `folder_id`
    - "gdrive-query":                           Search `query` in all drives
    - "gdrive-by-name":                         Search file with name `query`)
    - "gdrive-by-name-in-folder":               Search file with name `query`)
                                                 in `folder_id`
    - "gdrive-query-in-folder":                 Search `query` in `folder_id`
                                                 (and sub-folders in `recursive=true`)
    - "gdrive-mime-type":                       Search a specific `mime_type`
    - "gdrive-mime-type-in-folder":             Search a specific `mime_type` in
                                                 `folder_id`
    - "gdrive-query-with-mime-type":            Search `query` with a specific
                                                 `mime_type`
    - "gdrive-query-with-mime-type-and-folder": Search `query` with a specific
                                                 `mime_type` and in `folder_id`

    If you ask to use only the `description` of each file (mode='snippets'):
    - If a link has a description, use it
    - Else, use the description of the target_id file
    - If the description is empty, ignore the file
    ```
    Example:
        .. code-block:: python

        gdrive = GoogleDriveUtilities(
            gdrive_api_file=os.environ["GOOGLE_ACCOUNT_FILE"],
            num_results=10,
            template="gdrive-query-in-folder",
            recursive=True,
            filter=lambda search, file: "#ai" in file.get('description',''),
            folder_id='root',
            query='LLM',
            supportsAllDrives=False,
        )
        docs = gdrive.lazy_get_relevant_documents()
    ```
    """

    gdrive_api_file: Optional[FilePath]
    """
    The file to use to connect to the google api or use 
    `os.environ["GOOGLE_ACCOUNT_FILE"]`. May be a user or service json file"""

    not_data = uuid4()

    gdrive_token_path: Optional[Path] = None
    """ Path to save the token.json file. By default, use the directory of 
    `gdrive_api_file."""

    num_results: int = -1
    """Number of documents to be returned by the retriever (default: -1 for all)."""

    mode: str = "documents"
    """Return the document."""

    recursive: bool = False
    """If True, search in the `folder_id` and sub folders."""

    template: Union[
        PromptTemplate,
        Literal[
            "gdrive-all-in-folder",
            "gdrive-query",
            "gdrive-by-name",
            "gdrive-by-name-in-folder",
            "gdrive-query-in-folder",
            "gdrive-mime-type",
            "gdrive-mime-type-in-folder",
            "gdrive-query-with-mime-type",
            "gdrive-query-with-mime-type-and-folder",
        ],
        None,
    ] = None
    """
    A `PromptTemplate` with the syntax compatible with the parameter `q` 
    of Google API').
    The variables may be set in the constructor, or during the invocation of 
    `lazy_get_relevant_documents()`.
    """

    filter: Callable[["GoogleDriveUtilities", Dict], bool] = cast(
        Callable[["GoogleDriveUtilities", Dict], bool], lambda self, file: True
    )
    """ A lambda/function to add some filter about the google file item."""

    link_field: Literal["webViewLink", "webContentLink"] = "webViewLink"
    """Google API return two url for the same file.
      `webViewLink` is to open the document inline, and `webContentLink` is to
      download the document. Select the field to use for the documents."""

    follow_shortcut: bool = True
    """If `true` and find a google link to document or folder, follow it."""

    conv_mapping: dict = Field(default_factory=default_conv_loader)
    """A dictionary to map a mime-type and a loader"""

    gslide_mode: Literal["single", "elements", "slide"] = "single"
    """Generate one document by slide,
            one document with <PAGE BREAK> (`single`),
            one document by slide (`slide`)
            or one document for each `elements`."""

    gsheet_mode: Literal["single", "elements"] = "single"
    """Generate one document by line ("single"),
            or one document with markdown array and `<PAGE BREAK>` tags."""

    scopes: List[str] = SCOPES
    """ The scope to use the Google API. The default is for Read-only. 
    See [here](https://developers.google.com/identity/protocols/oauth2/scopes) """

    # Google Drive parameters
    corpora: Optional[Literal["user", "drive", "domain", "allDrives"]] = None
    """
    Groupings of files to which the query applies.
    Supported groupings are: 'user' (files created by, opened by, or shared directly 
    with the user),
    'drive' (files in the specified shared drive as indicated by the 'driveId'),
    'domain' (files shared to the user's domain), and 'allDrives' (A combination of 
    'user' and 'drive' for all drives where the user is a member).
    When able, use 'user' or 'drive', instead of 'allDrives', for efficiency."""

    driveId: Optional[str] = None
    """ID of the shared drive to search."""

    fields: str = (
        "id, name, mimeType, description, webViewLink, "
        "webContentLink, owners/displayName, shortcutDetails, "
        "sha256Checksum, modifiedTime"
    )
    """The paths of the fields you want included in the response.
        If not specified, the response includes a default set of fields specific to this
        method.
        For development, you can use the special value * to return all fields, but 
        you'll achieve greater performance by only selecting the fields you need. For 
        more information, see [Return specific fields for a file]
        (https://developers.google.com/drive/api/v3/fields-parameter)."""

    includeItemsFromAllDrives: Optional[bool] = False
    """Whether both My Drive and shared drive items should be included in results."""

    includeLabels: Optional[bool] = None
    """A comma-separated list of IDs of labels to include in the labelInfo part of 
    the response."""

    includePermissionsForView: Optional[Literal["published"]] = None
    """Specifies which additional view's permissions to include in the response.
    Only 'published' is supported."""

    orderBy: Optional[
        Literal[
            "createdTime",
            "folder",
            "modifiedByMeTime",
            "modifiedTime",
            "name",
            "name_natural",
            "quotaBytesUsed",
            "recency",
            "sharedWithMeTime",
            "starred",
            "viewedByMeTime",
        ]
    ] = None
    """
    A comma-separated list of sort keys. Valid keys are 'createdTime', 'folder', 
    'modifiedByMeTime', 'modifiedTime', 'name', 'name_natural', 'quotaBytesUsed', 
    'recency', 'sharedWithMeTime', 'starred', and 'viewedByMeTime'. Each key sorts 
    ascending by default, but may be reversed with the 'desc' modifier. 
    Example usage: `orderBy="folder,modifiedTime desc,name"`. Please note that there is
    a current limitation for users with approximately one million files in which the 
    requested sort order is ignored."""

    pageSize: int = 100
    """
    The maximum number of files to return per page. Partial or empty result pages are
    possible even before the end of the files list has been reached. Acceptable 
    values are 1 to 1000, inclusive."""

    spaces: Optional[Literal["drive", "appDataFolder"]] = None
    """A comma-separated list of spaces to query within the corpora. Supported values 
    are `drive` and `appDataFolder`."""

    supportsAllDrives: bool = True
    """Whether the requesting application supports both My Drives and
                shared drives. (Default: true)"""

    # Private fields
    _files = Field(allow_mutation=True)
    _docs = Field(allow_mutation=True)
    _spreadsheets = Field(allow_mutation=True)
    _slides = Field(allow_mutation=True)
    _creds = Field(allow_mutation=True)
    _gdrive_kwargs: Dict[str, Any] = Field(allow_mutation=True)
    _kwargs: Dict[str, Any] = Field(allow_mutation=True)
    _folder_name_cache: _LRUCache = Field(default_factory=_LRUCache)
    _not_supported: Set = Field(default_factory=set)
    _no_data: UUID = Field(default_factory=uuid4)

    # Class var
    _default_page_size: ClassVar[int] = 50

    _gdrive_list_params: ClassVar[Set[str]] = {
        "corpora",
        "corpus",
        "driveId",
        "fields",
        "includeItemsFromAllDrives",
        "includeLabels",
        "includePermissionsForView",
        "includeTeamDriveItems",
        "orderBy",
        "pageSize",
        "pageToken",
        "q",
        "spaces",
        "supportsAllDrives",
        "supportsTeamDrives",
        "teamDriveId",
    }
    _gdrive_get_params: ClassVar[Set[str]] = {
        "id",
        "acknowledgeAbuse",
        "fields",
        "includeLabels",
        "includePermissionsForView",
        "supportsAllDrives",
        "supportsTeamDrives",
    }

    def __init__(self, **kwargs: Any) -> None:
        from googleapiclient.discovery import build

        super().__init__(**kwargs)

        kwargs = {k: v for k, v in kwargs.items() if k not in self.__fields__}
        self._creds = self._load_credentials(Path(self.gdrive_api_file), self.scopes)
        self._files = build("drive", "v3", credentials=self._creds).files()
        self._docs = build("docs", "v1", credentials=self._creds).documents()
        self._spreadsheets = build(
            "sheets", "v4", credentials=self._creds
        ).spreadsheets()
        self._slides = build("slides", "v1", credentials=self._creds).presentations()

        # Gdrive parameters
        self._gdrive_kwargs = {
            "corpora": self.corpora,
            "driveId": self.driveId,
            "fields": self.fields,
            "includeItemsFromAllDrives": self.includeItemsFromAllDrives,
            "includeLabels": self.includeLabels,
            "includePermissionsForView": self.includePermissionsForView,
            "orderBy": self.orderBy,
            "pageSize": self.pageSize,
            "spaces": self.spaces,
            "supportsAllDrives": self.supportsAllDrives,
        }
        self._kwargs = kwargs
        self._folder_name_cache = _LRUCache()  # Cache with names of folders
        self._not_supported = set()  # Remember not supported mime type

    class Config:
        extra = Extra.allow
        underscore_attrs_are_private = True
        allow_mutation = False
        arbitrary_types_allowed = True

    @property
    def files(self) -> Any:
        """Google workspace files interface"""
        return self._files

    @validator("gdrive_api_file", always=True)
    def validate_api_file(cls, api_file: Optional[FilePath]) -> FilePath:
        if not api_file:
            env_api_file = os.environ.get("GOOGLE_ACCOUNT_FILE")
            if not env_api_file:
                raise ValueError("set GOOGLE_ACCOUNT_FILE environment variable")
            else:
                api_file = Path(env_api_file)
        else:
            if api_file is None:
                raise ValueError("gdrive_api_file must be set")
        if not api_file.exists():
            raise ValueError(f"Api file '{api_file}' does not exist")
        return api_file

    @root_validator
    def validate_template(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        template = values.get("template")
        if isinstance(template, str):
            template = get_template(template)
        if not template:
            raise ValueError("template must be set")
        values["template"] = template
        return values

    @root_validator
    def orderBy_is_compatible_with_recursive(
        cls, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        if values["orderBy"] and values["recursive"]:
            raise ValueError("`orderBy` is incompatible with `recursive` parameter")
        return values

    @root_validator
    def validate_folder_id_or_document_ids(
        cls, values: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Validate that either folder_id or document_ids is set, but not both."""
        if values.get("folder_id") and values.get("document_ids"):
            raise ValueError("folder_id or document_ids must be set")
        return values

    def _load_credentials(self, api_file: Optional[Path], scopes: List[str]) -> Any:
        """Load credentials.

         Args:
            api_file: The user or services json file

        Returns:
            credentials.
        """
        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            raise ImportError(
                "You must run "
                "`pip install --upgrade "
                "google-api-python-client google-auth-httplib2 "
                "google-auth-oauthlib` "
                "to use the Google Drive loader."
            )

        if api_file:
            with io.open(api_file, "r", encoding="utf-8-sig") as json_file:
                data = json.load(json_file)
            if "installed" in data:
                credentials_path = api_file
                service_account_key = None
            else:
                service_account_key = api_file
                credentials_path = None
        else:
            raise ValueError("Use GOOGLE_ACCOUNT_FILE env. variable.")

        # Implicit location of token.json
        if not self.gdrive_token_path and credentials_path:
            token_path: Optional[Path] = credentials_path.parent / "token.json"
        else:
            token_path = self.gdrive_token_path

        if service_account_key and service_account_key.exists():
            return service_account.Credentials.from_service_account_file(
                str(service_account_key), scopes=scopes
            )

        if token_path and token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        else:
            creds = None

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(credentials_path), scopes
                )
                creds = flow.run_local_server(port=0)
            if token_path:
                with open(token_path, "w") as token:
                    token.write(creds.to_json())

        return creds

    def _generate_missing_url(self, file: Dict) -> Optional[str]:
        """For Google document, create the corresponding URL"""
        mime_type = file["mimeType"]
        if mime_type.startswith("application/vnd.google-apps."):
            gdrive_document_type = mime_type.split(".")[-1]
            if self.link_field == "webViewLink":
                return (
                    f"https://docs.google.com/{gdrive_document_type}/d/"
                    f"{file['id']}/edit?usp=drivesdk"
                )
            else:
                return (
                    f"https://docs.google.com/{gdrive_document_type}/uc?"
                    f"{file['id']}&export=download"
                )
        return f"https://drive.google.com/file/d/{file['id']}?usp=share_link"

    def get_folder_name(self, file_id: str) -> str:
        """Return folder name from file_id. Cache the result."""
        name = self._folder_name_cache.get(file_id)
        if name:
            return name
        else:
            name = cast(str, self._get_file_by_id(file_id)["name"])
            self._folder_name_cache.put(file_id, name)
            return name

    def _get_file_by_id(self, file_id: str, **kwargs: Any) -> Dict:
        get_kwargs = {**self._kwargs, **kwargs, **{"fields": self.fields}}
        get_kwargs = {
            key: get_kwargs[key]
            for key in get_kwargs
            if key in GoogleDriveUtilities._gdrive_get_params
        }
        return self.files.get(fileId=file_id, **get_kwargs).execute()

    def _lazy_load_file_from_file(self, file: Dict) -> Iterator[Document]:
        """
        Load document from GDrive.
        Use the `conv_mapping` dictionary to convert different kind of files.
        """
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload

        suffix = mimetypes.guess_extension(file["mimeType"])
        if not suffix:
            suffix = Path(file["name"]).suffix
        if suffix not in self._not_supported:  # Already see this suffix?
            try:
                with tempfile.NamedTemporaryFile(mode="w", suffix=suffix) as tf:
                    path = tf.name
                    logger.debug(
                        f"Get '{file['name']}' with type "
                        f"'{file.get('mimeType', 'unknown')}'"
                    )

                    request = self.files.get_media(fileId=file["id"])

                    fh = io.FileIO(path, mode="wb")
                    try:
                        downloader = MediaIoBaseDownload(fh, request)
                        done = False
                        while done is False:
                            status, done = downloader.next_chunk()
                    finally:
                        fh.close()

                    if file["mimeType"] in self.conv_mapping:
                        logger.debug(
                            f"Try to convert '{file['name']}' with type "
                            f"'{file.get('mimeType', 'unknown')}'"
                        )
                        cls = self.conv_mapping[file["mimeType"]]
                        try:
                            documents = cls(file_path=path).load()
                            for i, document in enumerate(documents):
                                metadata = self._extract_meta_data(file)
                                if "source" in metadata:
                                    metadata["source"] = metadata["source"] + f"#_{i}"
                                document.metadata = metadata
                                yield document
                            return
                        except Exception as e:
                            logger.warning(
                                f"Exception during the conversion of file "
                                f"'{file['name']}' ({e})"
                            )
                            return
                    else:
                        logger.warning(
                            f"Ignore '{file['name']}' with type "
                            f"'{file.get('mimeType', 'unknown')}'"
                        )
                        self._not_supported.add(file["mimeType"])
                    return
            except HttpError:
                logger.warning(
                    f"Impossible to convert the file '{file['name']}' ({file['id']})"
                )
                self._not_supported.add(file["mimeType"])
                return

    def _export_google_workspace_document(self, file: Dict) -> Iterator[Document]:
        if file["mimeType"] == "application/vnd.google-apps.document":
            yield self._load_document_from_file(file)
        elif file["mimeType"] == "application/vnd.google-apps.spreadsheet":
            return self._lazy_load_sheets_from_file(file)
        elif file["mimeType"] == "application/vnd.google-apps.presentation":
            return self._lazy_load_slides_from_file(file)
        else:
            raise ValueError(f"mimeType `{file['mimeType']}` not supported")

    def _get_document(self, file: Dict, current_mode: str) -> Iterator[Document]:
        """Get text from file from Google Drive"""
        from googleapiclient.errors import HttpError

        mime_type = _extract_mime_type(file)
        file["mimeType"] = mime_type

        # Manage shortcut
        if mime_type == "application/vnd.google-apps.shortcut":
            if not self.follow_shortcut:
                return
            if "shortcutDetails" not in file:
                logger.debug("Breaking shortcut without target_id")
                return
            target_id = file["shortcutDetails"]["targetId"]
            target_mime_type = file["shortcutDetails"]["targetMimeType"]
            description = file.get("description", "").strip()
            target_file = {
                "id": target_id,
                "mimeType": target_mime_type,
                "name": file["name"],
                "description": description,
            }
            # Search the description of the target_id
            target = self.files.get(
                fileId=target_id, supportsAllDrives=True, fields=self.fields
            ).execute()
            target_file["description"] = target.get(
                "description", target_file["description"]
            )
            if "webViewLink" in target:
                target_file["webViewLink"] = target["webViewLink"]
            if "webContentLink" in target:
                target_file["webContentLink"] = target["webContentLink"]
            logger.debug(f"Manage link {target_file}")
            if not current_mode.startswith("snippets"):
                documents = self._get_document(target_file, current_mode)
                for document in documents:
                    document.metadata["gdriveId"] = file[
                        "id"
                    ]  # Inject the id of the shortcut
                    yield document
                return
            else:
                if not description:
                    return iter([])
                yield Document(
                    page_content=description,
                    metadata={**self._extract_meta_data(target), **{"id": file["id"]}},
                )
        else:
            target_mime_type = mime_type

            # Fix document URL
            if target_mime_type not in [
                "application/vnd.google-apps.shortcut",
                "application/vnd.google-apps.folder",
            ]:
                logger.debug(
                    f"Manage file '{file['name']}' ({file['id']} - "
                    f"{file.get('mimeType')}) "
                )
            document_url = file.get(self.link_field)
            if not document_url:
                document_url = self._generate_missing_url(file)
            if not document_url:
                logger.debug(f"Impossible to find the url for file '{file['name']}")
            file[self.link_field] = document_url

            # if used only the description of the files to generate documents
            if current_mode.startswith("snippets"):
                if target_mime_type == "application/vnd.google-apps.folder":
                    self._folder_name_cache.put(file["id"], file["name"])
                    return

                if not self.filter(self, file):
                    logger.debug(f"Filter reject the file '{file['name']}")
                    return

                description = file.get("description", "").strip()
                if not description:  # Description with nothing
                    logger.debug(f"Empty description. Ignore file {file['name']}")
                    return

                logger.debug(
                    f"For file '{file['name']}' use the description '{description}'"
                )
                metadata = self._extract_meta_data(file)
                if "summary" in metadata:
                    del metadata["summary"]
                document = Document(page_content=description, metadata=metadata)
                logger.debug(f"Return '{document.page_content[:40]}...'")
                yield document
                return

            if target_mime_type == "application/vnd.google-apps.folder":
                self._folder_name_cache.put(file["id"], file["name"])
                return

            # Try to convert, download and extract text
            if target_mime_type.startswith("application/vnd.google-apps."):
                try:
                    if self.filter(self, file):
                        for doc in self._export_google_workspace_document(file):
                            yield doc
                    else:
                        logger.debug(f"Filter reject the document {file['name']}")
                        return
                except HttpError:
                    logger.warning(
                        f"Impossible to read or convert the content "
                        f"of '{file['name']}'' ({file['id']}"
                    )
                    return iter([])
            else:
                if self.filter(self, file):
                    try:
                        suffix = mimetypes.guess_extension(file["mimeType"])
                        if not suffix:
                            suffix = Path(file["name"]).suffix
                        if suffix not in self._not_supported:
                            for doc in self._lazy_load_file_from_file(file):
                                yield doc
                        else:
                            logger.debug(
                                f"Ignore mime-type '{file['mimeType']}' for file "
                                f"'{file['name']}'"
                            )
                    except HttpError as x:
                        logger.debug(
                            f"*** During recursive search, "
                            f"for file {file['name']}, ignore error {x}"
                        )
                else:
                    logger.debug(f"File '{file['mimeType']}' refused by the filter.")

    def _extract_meta_data(self, file: Dict) -> Dict:
        """
        Extract metadata from file

        :param file: The file
        :return: Dict the meta data
        """
        meta = {
            "gdriveId": file["id"],
            "mimeType": file["mimeType"],
            "name": file["name"],
            "title": file["name"],
        }
        if file[self.link_field]:
            meta["source"] = file[self.link_field]
        else:
            logger.debug(f"Invalid URL {file}")
        if "createdTime" in file:
            meta["createdTime"] = file["createdTime"]
        if "modifiedTime" in file:
            meta["modifiedTime"] = file["modifiedTime"]
        if "sha256Checksum" in file:
            meta["sha256Checksum"] = file["sha256Checksum"]
        if "owners" in file:
            meta["author"] = file["owners"][0]["displayName"]
        if file.get("description", "").strip():
            meta["summary"] = file["description"]
        return meta

    def lazy_get_relevant_documents(
        self, query: Optional[str] = None, **kwargs: Any
    ) -> Iterator[Document]:
        """
        A generator to yield one document at a time.
        It's better for the memory.

        Args:
            query: Query string or None.
            kwargs: Additional parameters for templates of google list() api.

        Yield:
            Document
        """
        from googleapiclient.errors import HttpError

        if not query and "query" in self._kwargs:
            query = self._kwargs["query"]

        current_mode = kwargs.get("mode", self.mode)
        nb_yield = 0
        num_results = kwargs.get("num_results", self.num_results)
        if query is not None:
            # An empty query return all documents. But we want to return nothing.
            # We use a hack to replace the empty query to a random UUID.
            if not query:
                query = str(self.not_data)
            variables = {**self._kwargs, **kwargs, **{"query": query}}
        else:
            variables = {**self._kwargs, **kwargs}
        # Purge template variables
        variables = {
            k: v
            for k, v in variables.items()
            if k in cast(PromptTemplate, self.template).input_variables
        }
        query_str = (
            " " + "".join(cast(PromptTemplate, self.template).format(**variables)) + " "
        )
        list_kwargs = {
            **self._gdrive_kwargs,
            **kwargs,
            **{
                "pageSize": max(100, int(num_results * 1.5))
                if num_results > 0
                else GoogleDriveUtilities._default_page_size,
                "fields": f"nextPageToken, files({self.fields})",
                "q": query_str,
            },
        }
        list_kwargs = {
            k: v for k, v in list_kwargs.items() if k in _acceptable_params_of_list
        }

        folder_id = variables.get("folder_id")
        documents_id: Set[str] = set()
        recursive_folders = []
        visited_folders = []
        try:
            while True:  # Manage current folder
                next_page_token = None
                while True:  # Manage pages
                    list_kwargs["pageToken"] = next_page_token
                    logger.debug(f"{query_str=}, {next_page_token=}")
                    results = self.files.list(**list_kwargs).execute()
                    next_page_token, files = (
                        results.get("nextPageToken"),
                        results["files"],
                    )
                    for file in files:
                        file_key = (
                            file.get("webViewLink")
                            or file.get("webContentLink")
                            or file["id"]
                        )
                        if file_key in file in documents_id:
                            logger.debug(f"Already yield the document {file['id']}")
                            continue
                        documents = self._get_document(file, current_mode)
                        for i, document in enumerate(documents):
                            document_key = (
                                document.metadata.get("source")
                                or document.metadata["gdriveId"]
                            )
                            if document_key in documents_id:
                                # May by, with a link
                                logger.debug(
                                    f"Already yield the document '{document_key}'"
                                )
                                continue
                            documents_id.add(document_key)
                            nb_yield += 1
                            snippet = _snippet_from_page_content(document.page_content)
                            logger.info(
                                f"Yield '{document.metadata['name']}'-{i} with "
                                f'"{snippet}"'
                            )
                            yield document
                            if 0 < num_results == nb_yield:
                                break  # enough
                        if 0 < num_results == nb_yield:
                            break  # enough
                    if 0 < num_results == nb_yield:
                        break  # enough
                    if not next_page_token:
                        break
                if not self.recursive:
                    break  # Not _recursive folder

                if 0 < num_results == nb_yield:
                    break  # enough

                # ----------- Search sub-directories
                if not re.search(r"'([^']*)'\s+in\s+parents", query_str):
                    break
                visited_folders.append(folder_id)
                try:
                    if not folder_id:
                        raise ValueError(
                            "Set 'folder_id' if you use 'recursive == True'"
                        )

                    next_page_token = None
                    subdir_query = (
                        "(mimeType = 'application/vnd.google-apps.folder' "
                        "or mimeType = 'application/vnd.google-apps.shortcut') "
                        f"and '{folder_id}' in parents and trashed=false"
                    )
                    while True:  # Manage pages
                        logger.debug(f"Search in subdir '{subdir_query}'")
                        page_size = (
                            max(100, int(num_results * 1.5))
                            if num_results > 0
                            else self._default_page_size,
                        )
                        list_kwargs = {
                            **self._gdrive_kwargs,
                            **kwargs,
                            "pageSize": page_size,
                            "fields": "nextPageToken, "
                            "files(id,name, mimeType, shortcutDetails)",
                        }
                        # Purge list_kwargs
                        list_kwargs = {
                            key: list_kwargs[key]
                            for key in list_kwargs
                            if key in GoogleDriveUtilities._gdrive_list_params
                        }
                        results = self.files.list(
                            pageToken=next_page_token, q=subdir_query, **list_kwargs
                        ).execute()

                        next_page_token, files = (
                            results.get("nextPageToken"),
                            results["files"],
                        )
                        for file in files:
                            try:
                                mime_type = _extract_mime_type(file)
                                if mime_type == "application/vnd.google-apps.folder":
                                    recursive_folders.append(file["id"])
                                    self._folder_name_cache.put(
                                        file["id"], file["name"]
                                    )
                                    logger.debug(
                                        f"Add the folder "
                                        f"'{file['name']}' ({file['id']})"
                                    )
                                if (
                                    mime_type == "application/vnd.google-apps.shortcut"
                                    and self.follow_shortcut
                                ):
                                    # Manage only shortcut to folder
                                    if "shortcutDetails" in file:
                                        target_mimetype = file["shortcutDetails"][
                                            "targetMimeType"
                                        ]
                                        if (
                                            target_mimetype
                                            == "application/vnd.google-apps.folder"
                                        ):
                                            recursive_folders.append(
                                                file["shortcutDetails"]["targetId"]
                                            )
                                    else:
                                        logger.debug(
                                            f"Breaking shortcut '{file['name']}' "
                                            f"('{file['id']}') to a folder."
                                        )
                            except HttpError as x:
                                # Error when manage recursive directory
                                logger.debug(
                                    f"*** During recursive search, ignore error {x}"
                                )

                        if not next_page_token:
                            break

                    if not recursive_folders:
                        break

                    while True:
                        folder_id = recursive_folders.pop(0)
                        if folder_id not in visited_folders:
                            break
                    if not folder_id:
                        break

                    logger.debug(
                        f"Manage the folder '{self.get_folder_name(folder_id)}'"
                    )
                    # Update the parents folder and retry
                    query_str = re.sub(
                        r"'([^']*)'\s+in\s+parents",
                        f"'{folder_id}' in parents",
                        query_str,
                    )
                    list_kwargs["q"] = query_str
                except HttpError as x:
                    # Error when manage recursive directory
                    logger.debug(f"*** During recursive search, ignore error {x}")
                    traceback.print_exc()

        except HttpError as e:
            if "Invalid Value" in e.reason:
                raise
            logger.info(f"*** During google drive search, ignore error {e}")
            traceback.print_exc()

    def __del__(self) -> None:
        if hasattr(self, "_files") and self._files:
            self.files.close()
        if hasattr(self, "_docs") and self._docs:
            self._docs.close()
        if hasattr(self, "_spreadsheets") and self._spreadsheets:
            self._spreadsheets.close()
        if hasattr(self, "_slides") and self._slides:
            self._slides.close()

    @staticmethod
    def _extract_text(
        node: Any, key: str = "content", path: str = "/textRun"
    ) -> List[str]:
        result = []

        def visitor(node: Any, parent: str) -> None:
            if isinstance(node, dict):
                if key in node and isinstance(node.get(key), str):
                    if parent.endswith(path):
                        result.append(node[key].strip())
                for k, v in node.items():
                    visitor(v, parent + "/" + k)
            elif isinstance(node, list):
                for v in node:
                    visitor(v, parent + "/[]")

        visitor(node, "")
        return result

    def _lazy_load_sheets_from_file(self, file: Dict) -> Iterator[Document]:
        """Load a sheet and all tabs from an ID."""

        if file["mimeType"] != "application/vnd.google-apps.spreadsheet":
            logger.warning(f"File with id '{file['id']}' is not a GSheet")
            return
        spreadsheet = self._spreadsheets.get(spreadsheetId=file["id"]).execute()
        sheets = spreadsheet.get("sheets", [])
        single: List[str] = []

        for sheet in sheets:
            sheet_name = sheet["properties"]["title"]
            result = (
                self._spreadsheets.values()
                .get(spreadsheetId=file["id"], range=sheet_name)
                .execute()
            )
            values = result.get("values", [])

            width = max([len(v) for v in values])
            headers = values[0]
            if self.gsheet_mode == "elements":
                for i, row in enumerate(values[1:], start=1):
                    content = []
                    for j, v in enumerate(row):
                        title = (
                            str(headers[j]).strip() + ": " if len(headers) > j else ""
                        )
                        content.append(f"{title}{str(v).strip()}")

                    raw_content = "\n".join(content)
                    metadata = self._extract_meta_data(file)
                    if "source" in metadata:
                        metadata["source"] = (
                            metadata["source"]
                            + "#gid="
                            + str(sheet["properties"]["sheetId"])
                            + f"&{i}"
                        )

                    yield Document(page_content=raw_content, metadata=metadata)
            elif self.gsheet_mode == "single":
                lines = []
                line = "|"
                i = 0
                for i, head in enumerate(headers):
                    line += head + "|"
                for _ in range(i, width - 1):
                    line += " |"

                lines.append(line)
                line = "|"
                for _ in range(width):
                    line += "---|"
                lines.append(line)
                for row in values[1:]:
                    line = "|"
                    for i, v in enumerate(row):
                        line += str(v).strip() + "|"
                    for _ in range(i, width - 1):
                        line += " |"

                    lines.append(line)
                raw_content = "\n".join(lines)
                single.append(raw_content)
                yield Document(
                    page_content="\n<PAGE BREAK>\n".join(single),
                    metadata=self._extract_meta_data(file),
                )
            else:
                raise ValueError(f"Invalid mode '{self.gslide_mode}'")

    def _lazy_load_slides_from_file(self, file: Dict) -> Iterator[Document]:
        """Load a GSlide. Split each slide to different documents"""
        if file["mimeType"] != "application/vnd.google-apps.presentation":
            logger.warning(f"File with id '{file['id']}' is not a GSlide")
            return
        gslide = self._slides.get(presentationId=file["id"]).execute()
        if self.gslide_mode == "single":
            lines = []
            for slide in gslide["slides"]:
                if "pageElements" in slide:
                    page_elements = sorted(
                        slide["pageElements"],
                        key=lambda x: (
                            x["transform"].get("translateY", 0),
                            x["transform"].get("translateX", 0),
                        ),
                    )
                    lines += self._extract_text(page_elements)
                    lines.append("<PAGE BREAK>")
            if lines:
                lines = lines[:-1]
            yield Document(
                page_content="\n\n".join(lines), metadata=self._extract_meta_data(file)
            )
        elif self.gslide_mode == "slide":
            for slide in gslide["slides"]:
                if "pageElements" in slide:
                    page_elements = sorted(
                        slide["pageElements"],
                        key=lambda x: (
                            x["transform"].get("translateY", 0),
                            x["transform"].get("translateX", 0),
                        ),
                    )
                    meta = self._extract_meta_data(file).copy()
                    source = meta["source"]
                    if "#" in source:
                        source += f"&slide=id.{slide['objectId']}"
                    else:
                        source += f"#slide=id.{slide['objectId']}"
                    meta["source"] = source
                    yield Document(
                        page_content="\n\n".join(self._extract_text(page_elements)),
                        metadata=meta,
                    )
        elif self.gslide_mode == "elements":
            for slide in gslide["slides"]:
                metadata = self._extract_meta_data(file)
                if "source" in metadata:
                    metadata["source"] = (
                        metadata["source"] + "#slide=file_id." + slide["objectId"]
                    )
                for slide in gslide["slides"]:
                    if "pageElements" in slide:
                        page_elements = sorted(
                            slide["pageElements"],
                            key=lambda x: (
                                x["transform"].get("translateY", 0),
                                x["transform"].get("translateX", 0),
                            ),
                        )
                        for i, line in enumerate(self._extract_text(page_elements)):
                            if line.strip():
                                m = metadata.copy()
                                if "source" in m:
                                    m["source"] = m["source"] + f"&i={i}"

                                yield Document(page_content=line, metadata=m)
        else:
            raise ValueError(f"Invalid gslide_mode '{self.gslide_mode}'")

    def load_document_from_id(self, file_id: str) -> Document:
        file = self._get_file_by_id(file_id=file_id)
        return self._load_document_from_file(file)

    def _load_document_from_file(self, file: dict) -> Document:
        if file["mimeType"] != "application/vnd.google-apps.document":
            raise ValueError(f"File with id '{file['id']}' is not a GDoc")
        gdoc = self._docs.get(documentId=file["id"]).execute()
        text = "\n\n".join(self._extract_text(gdoc["body"]["content"]))
        return Document(page_content=text, metadata=self._extract_meta_data(file))

    def load_slides_from_id(self, file_id: str) -> List[Document]:
        """Load a GSlide."""
        return list(self.lazy_load_slides_from_id(file_id))

    def lazy_load_slides_from_id(self, file_id: str) -> Iterator[Document]:
        file = self._get_file_by_id(file_id=file_id)
        return self._lazy_load_slides_from_file(file)

    def load_sheets_from_id(self, file_id: str) -> List[Document]:
        """Load a GSheets."""
        return list(self.lazy_load_sheets_from_id(file_id))

    def lazy_load_sheets_from_id(self, file_id: str) -> Iterator[Document]:
        """Load a GSheets."""
        file = self._get_file_by_id(file_id=file_id)
        return self._lazy_load_sheets_from_file(file)

    def lazy_load_file_from_id(self, file_id: str) -> Iterator[Document]:
        return self._get_document(
            self._get_file_by_id(file_id=file_id), current_mode=self.mode
        )

    def load_file_from_id(self, file_id: str) -> List[Document]:
        """Load file from GDrive"""
        return list(self.lazy_load_file_from_id(file_id))


class GoogleDriveAPIWrapper(GoogleDriveUtilities):
    """
    Search on Google Drive.
    By default, search in filename only.
    Use a specific template if you want a different approach.
    """

    class Config:
        extra = Extra.allow
        underscore_attrs_are_private = True
        allow_mutation = False

    mode: Literal[
        "snippets", "snippets-markdown", "documents", "documents-markdown"
    ] = "snippets-markdown"

    num_results: int = 10
    """ Number of results """

    template: Union[
        PromptTemplate,
        Literal[
            "gdrive-all-in-folder",
            "gdrive-query",
            "gdrive-by-name",
            "gdrive-by-name-in-folder",
            "gdrive-query-in-folder",
            "gdrive-mime-type",
            "gdrive-mime-type-in-folder",
            "gdrive-query-with-mime-type",
            "gdrive-query-with-mime-type-and-folder",
        ],
        None,
    ] = "gdrive-query"

    @root_validator(pre=True)
    def validate_template(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        folder_id = v.get("folder_id")

        if "template" not in v:
            if folder_id:
                template = get_template("gdrive-by-name-in-folder")
            else:
                template = get_template("gdrive-by-name")
            v["template"] = template
        return v

    def run(self, query: str) -> str:
        """Run query through Google Drive and parse result."""
        snippets = []
        logger.debug(f"{query=}")
        for document in self.lazy_get_relevant_documents(
            query=query, num_results=self.num_results
        ):
            content = document.page_content
            if (
                self.mode in ["snippets", "snippets-markdown"]
                and "summary" in document.metadata
                and document.metadata["summary"]
            ):
                content = document.metadata["summary"]
            if self.mode == "snippets":
                snippets.append(
                    f"Name: {document.metadata['name']}\n"
                    f"Source: {document.metadata['source']}\n" + f"Summary: {content}"
                )
            elif self.mode == "snippets-markdown":
                snippets.append(
                    f"[{document.metadata['name']}]"
                    f"({document.metadata['source']})<br/>\n" + f"{content}"
                )
            elif self.mode == "documents":
                snippet = _snippet_from_page_content(content)
                snippets.append(
                    f"Name: {document.metadata['name']}\n"
                    f"Source: {document.metadata['source']}\n" + f"Summary: "
                    f"{snippet}"
                )
            elif self.mode == "documents-markdown":
                snippet = GoogleDriveUtilities._snippet_from_page_content(content)
                snippets.append(
                    f"[{document.metadata['name']}]"
                    f"({document.metadata['source']})<br/>" + f"{snippet}"
                )
            else:
                raise ValueError(f"Invalid mode `{self.mode}`")

        if not len(snippets):
            return "No document found"

        return "\n\n".join(snippets)

    def results(self, query: str, num_results: int) -> List[Dict]:
        """Run query through Google Drive and return metadata.

        Args:
            query: The query to search for.
            num_results: The number of results to return.

        Returns:
            Like bing_search, a list of dictionaries with the following keys:
                `snippet: The `description` of the result.
                `title`: The title of the result.
                `link`: The link to the result.
        """
        metadata_results = []
        for document in self.lazy_get_relevant_documents(
            query=query, num_results=num_results
        ):
            metadata_result = {
                "title": document.metadata["name"],
                "link": document.metadata["source"],
            }
            if "summary" in document.metadata:
                metadata_result["snippet"] = document.metadata["summary"]
            else:
                metadata_result["snippet"] = _snippet_from_page_content(
                    document.page_content
                )
            metadata_results.append(metadata_result)
        if not metadata_results:
            return [{"Result": "No good Google Drive Search Result was found"}]

        return metadata_results

    def get_format_instructions(self) -> str:
        """Return format instruction for LLM"""
        return FORMAT_INSTRUCTION
