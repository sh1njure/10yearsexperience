"""Thin async client for the PrestaShop legacy Webservice API.

Design rules enforced here (see project brief):

* Auth is HTTP Basic — the Webservice key is the username, password is empty.
* Never hand-build XML. Always fetch ``?schema=blank`` and fill that skeleton.
* Read-only fields are stripped before POST/PUT.
* Multilingual fields are wrapped as ``<language id="N">value</language>``.

The client is intentionally transport-only: it knows how to talk to the API and
turn schemas into payloads, but business rules (mapping, validation, import
orchestration) live in the other modules.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import httpx

# The PrestaShop namespace declared on the root <prestashop> element.
PS_NS = "http://www.w3.org/1999/xlink"

# Fields the API generates/manages itself and rejects on write. These are
# stripped from every payload before POST/PUT. The list is deliberately broad;
# schema fetching also flags read-only fields, and both are honoured.
READ_ONLY_FIELDS = {
    "id",
    "manufacturer_name",
    "quantity",  # on products; stock is set via stock_availables
    "position_in_category",
    "date_add",
    "date_upd",
    "supplier_name",
}


class PrestaShopError(Exception):
    """Raised when the API returns an error or an unexpected response."""

    def __init__(self, message: str, status_code: int | None = None,
                 body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass
class SchemaField:
    """One field from a ``?schema=blank`` response."""

    name: str
    required: bool = False
    read_only: bool = False
    multilingual: bool = False
    max_size: int | None = None


@dataclass
class ResourceSchema:
    resource: str
    fields: list[SchemaField] = field(default_factory=list)

    @property
    def writable_fields(self) -> list[SchemaField]:
        return [
            f for f in self.fields
            if not f.read_only and f.name not in READ_ONLY_FIELDS
        ]

    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


def _localname(tag: str) -> str:
    """Strip an XML namespace prefix from a tag name."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


class PrestaShopClient:
    """Async PrestaShop Webservice client.

    Usage::

        async with PrestaShopClient(url, api_key) as client:
            resources = await client.test_connection()
    """

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0,
                 default_lang_id: int = 1, max_retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_lang_id = default_lang_id
        self.max_retries = max_retries
        # Key as username, empty password.
        self._auth = httpx.BasicAuth(api_key, "")
        self._client = httpx.AsyncClient(
            auth=self._auth,
            timeout=timeout,
            follow_redirects=True,
        )

    async def __aenter__(self) -> "PrestaShopClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Low level                                                          #
    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self.base_url}/api/{path}" if not path.startswith("api/") \
            else f"{self.base_url}/{path}"

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       content: bytes | str | None = None,
                       headers: dict | None = None,
                       files: dict | None = None) -> httpx.Response:
        url = self._url(path)
        # Idempotent reads are retried on transient 5xx (a local PrestaShop can
        # briefly 500 under a burst of lookups). Writes are retried at a higher
        # level (importer) so we don't risk double-creating here.
        retryable = method.upper() in ("GET", "HEAD")
        delay = 1.0
        attempts = self.max_retries + 1 if retryable else 1
        for attempt in range(attempts):
            try:
                resp = await self._client.request(
                    method, url, params=params, content=content,
                    headers=headers, files=files,
                )
            except httpx.HTTPError as exc:  # network / TLS / timeout
                raise PrestaShopError(f"Request to {url} failed: {exc}") from exc

            if resp.status_code == 401:
                raise PrestaShopError(
                    "Authentication failed (401). Check the Webservice API key "
                    "and that the Webservice is enabled.",
                    status_code=401, body=resp.text,
                )
            if retryable and 500 <= resp.status_code < 600 and attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return resp

    @staticmethod
    def _raise_for_ps_errors(resp: httpx.Response) -> None:
        """Turn a PrestaShop error response into a readable exception."""
        if resp.status_code < 400:
            return
        message = PrestaShopClient.extract_error_message(resp.text)
        if not message:
            body = (resp.text or "").strip()
            # PrestaShop 5xx returns a full HTML page — don't dump it.
            if body[:15].lower().startswith(("<!doctype", "<html")):
                message = "server error (HTML page returned, not an API response)"
            else:
                message = body[:300] or resp.reason_phrase
        path = ""
        try:
            path = resp.request.url.path
        except Exception:
            pass
        where = f" on {path}" if path else ""
        raise PrestaShopError(
            f"PrestaShop returned {resp.status_code}{where}: {message}",
            status_code=resp.status_code,
            body=resp.text,
        )

    @staticmethod
    def extract_error_message(body: str) -> str | None:
        """Pull the human-readable message out of a PrestaShop XML error body."""
        if not body:
            return None
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        messages = []
        for msg in root.iter():
            if _localname(msg.tag) == "message" and msg.text:
                messages.append(msg.text.strip())
        return "; ".join(messages) if messages else None

    # ------------------------------------------------------------------ #
    # Connection test                                                    #
    # ------------------------------------------------------------------ #
    async def test_connection(self) -> list[str]:
        """Hit ``GET /api/`` and return the resources the key can access.

        The root endpoint lists every resource the key is permitted to touch as
        child elements of ``<api>``. Returns a sorted list of resource names.
        Raises :class:`PrestaShopError` on auth/network failure.
        """
        resp = await self._request("GET", "")
        self._raise_for_ps_errors(resp)
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            raise PrestaShopError(
                "Root endpoint did not return valid XML — is this a PrestaShop "
                "Webservice URL?",
                status_code=resp.status_code,
                body=resp.text[:500],
            ) from exc

        api = root if _localname(root.tag) == "api" else root.find(".//api")
        resources: list[str] = []
        if api is not None:
            for child in api:
                name = _localname(child.tag)
                if name:
                    resources.append(name)
        return sorted(set(resources))

    # ------------------------------------------------------------------ #
    # Schema                                                             #
    # ------------------------------------------------------------------ #
    async def fetch_schema(self, resource: str) -> ResourceSchema:
        """Fetch and parse ``GET /api/{resource}?schema=blank``.

        Field names are read live from the shop — nothing is hardcoded.
        """
        resp = await self._request("GET", resource, params={"schema": "blank"})
        self._raise_for_ps_errors(resp)
        return self.parse_schema(resource, resp.text)

    @staticmethod
    def parse_schema(resource: str, xml_text: str) -> ResourceSchema:
        """Parse a blank-schema XML document into a :class:`ResourceSchema`.

        Pure function (no I/O) so it can be unit-tested against fixtures.
        """
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise PrestaShopError(f"Could not parse schema for {resource}: {exc}")

        # The resource element is the single child of <prestashop>.
        resource_el = next(iter(root), None)
        if resource_el is None:
            return ResourceSchema(resource=resource, fields=[])

        fields: list[SchemaField] = []
        for el in resource_el:
            name = _localname(el.tag)
            attrs = {_localname(k): v for k, v in el.attrib.items()}
            # Multilingual fields contain <language> children in the skeleton.
            multilingual = any(
                _localname(child.tag) == "language" for child in el
            )
            max_size = attrs.get("maxSize")
            fields.append(
                SchemaField(
                    name=name,
                    required=attrs.get("required") == "true",
                    read_only=attrs.get("readOnly") == "true",
                    multilingual=multilingual,
                    max_size=int(max_size) if max_size and max_size.isdigit()
                    else None,
                )
            )
        return ResourceSchema(resource=resource, fields=fields)

    # ------------------------------------------------------------------ #
    # Reads                                                              #
    # ------------------------------------------------------------------ #
    async def get_json(self, resource: str, *, params: dict | None = None) -> dict:
        """GET a resource as JSON (``output_format=JSON``)."""
        params = {**(params or {}), "output_format": "JSON"}
        resp = await self._request("GET", resource, params=params)
        self._raise_for_ps_errors(resp)
        if not resp.text.strip():
            return {}
        return resp.json()

    async def get_xml(self, resource: str, *, params: dict | None = None) -> str:
        """GET a resource as raw XML (needed to round-trip a full resource)."""
        resp = await self._request("GET", resource, params=params)
        self._raise_for_ps_errors(resp)
        return resp.text

    # ------------------------------------------------------------------ #
    # Writes                                                             #
    # ------------------------------------------------------------------ #
    async def create(self, resource: str, xml_payload: str) -> dict:
        """POST an XML payload; return the parsed created resource summary."""
        resp = await self._request(
            "POST", resource, content=xml_payload.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
        )
        self._raise_for_ps_errors(resp)
        return {"status_code": resp.status_code, "body": resp.text,
                "id": self._extract_id(resp.text)}

    async def update(self, resource: str, resource_id: int, xml_payload: str) -> dict:
        """PUT a full XML payload back for an existing resource."""
        resp = await self._request(
            "PUT", f"{resource}/{resource_id}",
            content=xml_payload.encode("utf-8"),
            headers={"Content-Type": "text/xml"},
        )
        self._raise_for_ps_errors(resp)
        return {"status_code": resp.status_code, "body": resp.text,
                "id": resource_id}

    async def upload_image(self, product_id: int, image_bytes: bytes,
                           filename: str, content_type: str = "image/jpeg") -> dict:
        """POST an image as multipart to ``/api/images/products/{id}``."""
        files = {"image": (filename, image_bytes, content_type)}
        resp = await self._request(
            "POST", f"images/products/{product_id}", files=files,
        )
        self._raise_for_ps_errors(resp)
        return {"status_code": resp.status_code, "body": resp.text,
                "id": self._extract_id(resp.text)}

    @staticmethod
    def _extract_id(body: str) -> int | None:
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        for el in root.iter():
            if _localname(el.tag) == "id" and el.text and el.text.isdigit():
                return int(el.text)
        return None
