"""
Meta Vision Service for E-commerce Product Identification & Catalog Matching.

Hackathon edition: image understanding runs on the Meta Model API with the
same META_MODEL_API_KEY as chat. No third-party vision provider is used.

Features:
- Resilient MetaVisionApiClient with exponential backoff and model fallback.
- Dynamic MIME type detection via magic bytes and HTTP headers.
- Strict Pydantic parsing and validation schemas ensuring all model JSON outputs are strictly validated before processing.
- Clean formatting helper format_vision_context for customer-facing sales attendant LLM responses.
"""

import base64
import json
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from pydantic import BaseModel, Field, field_validator, ValidationError

import config

logger = logging.getLogger(__name__)

# Constants
DEFAULT_TIMEOUT_SECONDS = 5.0 if getattr(config, "is_serverless", lambda: False)() else 10.0
MAX_IMAGE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB limit
# Meta Model API (OpenAI-compatible chat completions with image input).


# ---------------------------------------------------------------------------
# Strict Pydantic Parsing & Output Schemas
# ---------------------------------------------------------------------------

class MetaVisionOutputSchema(BaseModel):
    """
    Strict validation schema for the structured JSON response returned by Meta Vision.
    Enforces required fields, handles type coercion, and sanitizes strings.
    """
    is_electronics: bool = Field(
        ...,
        description="True if the image clearly portrays an electronics device, tech accessory, computer hardware, or gadget."
    )
    product_name: Optional[str] = Field(
        default=None,
        description="Exact product name or most specific model line identified."
    )
    brand: Optional[str] = Field(
        default=None,
        description="Manufacturer / brand name (e.g. MSI, Apple, Samsung, Sony)."
    )
    category: Optional[str] = Field(
        default=None,
        description="Electronics category (e.g. Laptops, Smartphones, Audio & Headphones)."
    )
    color: Optional[str] = Field(
        default=None,
        description="Physical finish or dominant exterior color."
    )
    specs_summary: Optional[str] = Field(
        default=None,
        description="Key visual features, ports, screen type, or technical specifications."
    )
    search_query: Optional[str] = Field(
        default=None,
        description="Optimal 2-4 word keyword search string for catalog lookup."
    )
    description: Optional[str] = Field(
        default=None,
        description="Concise description of the product in the image."
    )
    message: Optional[str] = Field(
        default=None,
        description="Explanation if the item is not an electronics product or cannot be identified."
    )

    @field_validator("is_electronics", mode="before")
    @classmethod
    def parse_bool(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            clean = v.strip().lower()
            if clean in ("true", "1", "yes", "y", "t"):
                return True
            if clean in ("false", "0", "no", "n", "f"):
                return False
        return bool(v)

    @field_validator("product_name", "brand", "category", "color", "specs_summary", "search_query", "description", "message", mode="before")
    @classmethod
    def clean_strings(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None


class VisionAnalysisSuccess(BaseModel):
    """Strict schema for a successful product identification result dictionary."""
    success: bool = True
    product_name: str = Field(..., min_length=1)
    brand: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    color: str = Field(..., min_length=1)
    specs_summary: str = Field(default="")
    search_query: str = Field(..., min_length=1)
    description: str = Field(default="")


class VisionAnalysisFailure(BaseModel):
    """Strict schema for an unsuccessful / non-electronics identification result dictionary."""
    success: bool = False
    message: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Image Downloading & Dynamic MIME Type Detection
# ---------------------------------------------------------------------------

def detect_mime_type(data: bytes, header_mime: Optional[str] = None) -> str:
    """
    Dynamically detects image MIME type using file magic bytes,
    falling back to Content-Type header or image/jpeg default.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and (data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1")):
        return "image/heif"

    if header_mime and header_mime.startswith("image/"):
        return header_mime.split(";")[0].strip().lower()

    return "image/jpeg"


def download_image_bytes(
    image_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_size: int = MAX_IMAGE_SIZE_BYTES
) -> Tuple[bytes, str]:
    """
    Downloads raw image bytes from an HTTP/HTTPS URL or decodes a data URI.
    
    Returns:
        Tuple of (image_bytes, mime_type)
        
    Raises:
        ValueError, ConnectionError, or Timeout on failure.
    """
    clean_url = (image_url or "").strip()
    if not clean_url:
        raise ValueError("Image URL is empty.")

    # 1. Handle base64 Data URIs directly (e.g. data:image/jpeg;base64,...)
    if clean_url.startswith("data:"):
        header, _, encoded = clean_url.partition(",")
        if not encoded:
            raise ValueError("Invalid data URI: missing payload.")
        mime_type = "image/jpeg"
        if ";" in header:
            type_part = header.split(";")[0]
            if ":" in type_part:
                detected = type_part.split(":")[1].strip().lower()
                if detected:
                    mime_type = detected
        try:
            raw_bytes = base64.b64decode(encoded)
        except Exception as e:
            raise ValueError(f"Failed to decode base64 data URI: {e}")
        if len(raw_bytes) > max_size:
            raise ValueError(f"Image exceeds size limit of {max_size // (1024 * 1024)}MB.")
        return raw_bytes, mime_type

    # 2. Handle HTTP/HTTPS URL (SSRF guard: https only, no private hosts)
    from urllib.parse import urlparse
    import ipaddress
    import socket
    parsed = urlparse(clean_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only https image URLs are allowed.")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        raise ValueError("Blocked non-public image host.")
    try:
        for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)[:4]:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("Blocked private image host.")
    except ValueError:
        raise
    except Exception:
        pass  # DNS hiccup: let the request below fail naturally
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }

    try:
        with requests.get(clean_url, headers=headers, timeout=timeout, stream=True) as resp:
            resp.raise_for_status()

            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > max_size:
                raise ValueError(f"Image content-length exceeds limit of {max_size // (1024 * 1024)}MB.")

            chunks = []
            downloaded = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if downloaded > max_size:
                        raise ValueError(f"Image payload exceeds maximum size limit of {max_size // (1024 * 1024)}MB.")

            raw_bytes = b"".join(chunks)
            if not raw_bytes:
                raise ValueError("Downloaded image payload is empty (0 bytes).")

            mime_type = detect_mime_type(raw_bytes, resp.headers.get("Content-Type"))
            return raw_bytes, mime_type

    except requests.exceptions.Timeout as e:
        raise TimeoutError(f"Image download timed out after {timeout} seconds: {e}")
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Failed to download image from URL: {e}")


# ---------------------------------------------------------------------------
# Resilient API Client Manager with Exponential Backoff
# ---------------------------------------------------------------------------

class MetaVisionApiClient:
    """
    Vision client for the Meta Model API (chat completions + image input).

    Uses the same META_MODEL_API_KEY as the chat service. Exponential
    backoff with jitter on 429/5xx, then falls back to the next Meta model.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        primary_model: Optional[str] = None,
        max_retries: Optional[int] = None,
        base_delay: float = 1.0,
        max_delay: float = 5.0,
        backoff_factor: float = 2.0,
        request_timeout: Optional[float] = None
    ):
        serverless = getattr(config, "is_serverless", lambda: False)()
        self.api_key = api_key or getattr(config, "META_MODEL_API_KEY", "")
        self.base_url = (getattr(config, "META_MODEL_API_BASE", "https://api.meta.ai/v1") or "https://api.meta.ai/v1").rstrip("/")
        self.primary_model = primary_model or getattr(config, "META_VISION_MODEL", "muse-spark-1.3")
        self.max_retries = max_retries if max_retries is not None else (1 if serverless else 2)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.request_timeout = request_timeout if request_timeout is not None else (8.0 if serverless else 15.0)

        models_order = [self.primary_model, "muse-spark-1.2", "muse-spark-1.1"]
        seen = set()
        self.candidate_models = []
        for m in models_order:
            if m and m not in seen:
                seen.add(m)
                self.candidate_models.append(m)

        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=5, pool_maxsize=10, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _calculate_backoff_delay(self, attempt: int, resp: Optional[requests.Response] = None) -> float:
        """Exponential backoff with jitter, respecting Retry-After."""
        if resp is not None:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(0.5, float(retry_after))
                except ValueError:
                    pass
        calculated = min(self.max_delay, self.base_delay * (self.backoff_factor ** attempt))
        return calculated + random.uniform(0.1, 0.5)

    def generate_content(
        self,
        prompt: str,
        image_bytes: bytes,
        mime_type: str = "image/jpeg"
    ) -> dict:
        """One Meta Model API chat completion with the image attached."""
        if not self.api_key:
            raise ValueError("META_MODEL_API_KEY is not configured.")

        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64_data}"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_exception = None

        for model in self.candidate_models:
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": model,
                "temperature": 0.1,
                "max_tokens": 1024,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }],
            }
            logger.info(f"Attempting Meta Vision request using model '{model}'...")
            for attempt in range(self.max_retries):
                try:
                    resp = self._session.post(url, json=payload, headers=headers,
                                              timeout=self.request_timeout)
                    if resp.status_code == 200:
                        return resp.json()
                    if resp.status_code in (429, 500, 502, 503, 504):
                        delay = self._calculate_backoff_delay(attempt, resp)
                        logger.warning(f"Meta Vision {resp.status_code} on '{model}'; retry in {delay:.2f}s")
                        time.sleep(delay)
                        last_exception = RuntimeError(f"Meta Vision {resp.status_code}: {resp.text[:200]}")
                        continue
                    last_exception = RuntimeError(f"Meta Vision error ({resp.status_code}) on '{model}': {resp.text[:200]}")
                    logger.error(str(last_exception))
                    break
                except requests.exceptions.RequestException as req_err:
                    delay = self._calculate_backoff_delay(attempt)
                    logger.warning(f"Network exception on '{model}': {req_err}; retry in {delay:.2f}s")
                    time.sleep(delay)
                    last_exception = req_err

        raise RuntimeError(f"All Meta Vision models failed. Last error: {last_exception}")


_client_instance: Optional[MetaVisionApiClient] = None
_client_lock = threading.Lock()


def get_vision_client() -> MetaVisionApiClient:
    """Returns thread-safe singleton instance of MetaVisionApiClient."""
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:
                _client_instance = MetaVisionApiClient()
    return _client_instance


# ---------------------------------------------------------------------------
# Expert E-Commerce Vision Prompt
# ---------------------------------------------------------------------------

VISION_PROMPT = """You are an expert e-commerce vision assistant for a premium electronics and gadgets retail store.
Analyze the customer\'s uploaded image with extreme precision and attention to detail.

OBJECTIVE:
1. Determine whether the image clearly depicts a consumer electronics product, tech gadget, computer hardware, or electronic accessory.
2. If the image is NOT an electronic product/gadget (e.g. food, pets, random scenery, human face without visible gadgets, clothes, furniture) or is completely blurry/unrecognizable:
   - "is_electronics": false
   - "message": A polite, concise explanation stating that the image does not show a recognizable electronics product.
   - Set all other fields to null.

3. If the image IS an electronic product:
   - "is_electronics": true
   - "product_name": The exact, specific brand and model name visible or identifiable (e.g. "MSI Stealth 14 Studio", "Samsung Odyssey OLED G9", "Sony WH-1000XM5", "Apple MacBook Air M2", "Asus ROG Zephyrus G14"). If exact sub-model revision is ambiguous, identify the most accurate core model series.
   - "brand": Primary manufacturer/brand name (e.g. "MSI", "Samsung", "Sony", "Apple", "Asus", "Dell", "HP", "Lenovo", "Logitech").
   - "category": Best matching electronics category from:
     ["Laptops", "Smartphones", "Audio & Headphones", "Smartwatches & Fitness", "Cameras & Photography", "PC Components & Gaming", "Monitors & Displays", "Tablets", "Accessories & Peripherals", "Smart Home & Appliances"].
   - "color": Dominant physical exterior casing color / finish (e.g. "Star Blue", "Space Gray", "Carbon Black", "White", "Silver", "Midnight").
   - "specs_summary": Key visible specifications, form factor highlights, ports, screen type, or technical characteristics.
   - "search_query": An optimal keyword query string strictly 2 to 4 words containing Brand and Model (e.g. "MSI Stealth 14", "Samsung Odyssey OLED 27", "Sony WH 1000XM5", "Apple MacBook Air"). DO NOT include filler words like "buy", "specs", "silver", or "laptop".
   - "description": A concise, natural 1-2 sentence description of what product is shown in the image.
   - "message": null

Strictly return a valid JSON object matching this schema:
{
  "is_electronics": true,
  "product_name": "...",
  "brand": "...",
  "category": "...",
  "color": "...",
  "specs_summary": "...",
  "search_query": "...",
  "description": "...",
  "message": null
}"""


def _extract_json_payload(raw_text: str) -> dict:
    """Extracts and parses JSON object from LLM response, handling markdown blocks or formatting quirks."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            return json.loads(match.group(1))
        raise


# ---------------------------------------------------------------------------
# Core Service Functions
# ---------------------------------------------------------------------------

def analyze_product_image(image_url: str) -> dict:
    """
    Downloads an image from image_url and performs visual analysis via the Meta Model API.
    
    Validates output using strict Pydantic schemas.
    
    Returns:
        Structured dictionary:
        If successful:
        {
            "success": True,
            "product_name": "...",
            "brand": "...",
            "category": "...",
            "color": "...",
            "specs_summary": "...",
            "search_query": "...",
            "description": "..."
        }
        
        If image cannot be identified or is not a product/electronics item:
        {
            "success": False,
            "message": "..."
        }
    """
    if not image_url or not isinstance(image_url, str) or not image_url.strip():
        return VisionAnalysisFailure(
            message="No image URL was provided for analysis."
        ).model_dump()

    try:
        image_bytes, mime_type = download_image_bytes(image_url)
    except Exception as e:
        logger.warning(f"Vision Service failed to download image: {e}")
        return VisionAnalysisFailure(
            message=f"Failed to load product image: {str(e)}"
        ).model_dump()

    try:
        client = get_vision_client()
        api_response = client.generate_content(
            prompt=VISION_PROMPT,
            image_bytes=image_bytes,
            mime_type=mime_type
        )
    except Exception as e:
        logger.error(f"Meta Vision API execution error: {e}", exc_info=True)
        return VisionAnalysisFailure(
            message="Unable to communicate with the vision service. Please try again or provide the product name."
        ).model_dump()

    choices = api_response.get("choices") or []
    raw_text = ""
    if choices:
        _content = (choices[0].get("message") or {}).get("content")
        if isinstance(_content, str):
            raw_text = _content
        elif isinstance(_content, list):
            raw_text = "".join(str(part.get("text") or "") for part in _content if isinstance(part, dict))
    if not raw_text.strip():
        logger.warning("Meta Vision returned empty content.")
        return VisionAnalysisFailure(
            message="The vision model did not generate any analysis for this image."
        ).model_dump()

    try:
        parsed_json = _extract_json_payload(raw_text)
        validated_llm = MetaVisionOutputSchema.model_validate(parsed_json)
    except (json.JSONDecodeError, ValidationError) as parse_err:
        logger.error(f"Failed to validate LLM response against Pydantic schema: {parse_err}. Raw text: {raw_text[:200]}")
        return VisionAnalysisFailure(
            message="Failed to parse visual identification output into structured schema."
        ).model_dump()

    if not validated_llm.is_electronics or not validated_llm.product_name:
        default_rejection = (
            "The image does not appear to be a consumer electronics product or gadget. "
            "Please send a clear photo of an electronic device, laptop, phone, or accessory."
        )
        return VisionAnalysisFailure(
            message=validated_llm.message or default_rejection
        ).model_dump()

    search_q = validated_llm.search_query
    if not search_q:
        brand = validated_llm.brand or ""
        pname = validated_llm.product_name or ""
        search_q = f"{brand} {pname}".strip() if brand.lower() not in pname.lower() else pname

    try:
        success_result = VisionAnalysisSuccess(
            success=True,
            product_name=validated_llm.product_name,
            brand=validated_llm.brand or "Unknown",
            category=validated_llm.category or "Electronics",
            color=validated_llm.color or "Standard finish",
            specs_summary=validated_llm.specs_summary or "",
            search_query=search_q,
            description=validated_llm.description or f"Identified {validated_llm.product_name}."
        )
        return success_result.model_dump()
    except ValidationError as val_err:
        logger.error(f"Error constructing VisionAnalysisSuccess: {val_err}")
        return VisionAnalysisFailure(
            message="Internal error structuring vision analysis results."
        ).model_dump()


def format_vision_context(vision_data: dict, catalog_matches: Optional[list] = None) -> str:
    """
    Formats the Meta vision finding and store match for the final LLM response generator.
    
    Args:
        vision_data: Result dictionary from analyze_product_image().
        catalog_matches: Optional list of catalog matches (either product dicts or (product_dict, score) tuples).
        
    Returns:
        Clean, structured prompt context string ready to be passed into the conversational sales LLM.
    """
    if not vision_data or not vision_data.get("success"):
        notice = vision_data.get("message", "The image could not be identified.") if vision_data else "No image data."
        lines = [
            "=== CUSTOMER PHOTO ANALYSIS (UNIDENTIFIED / NON-ELECTRONICS) ===",
            f"Notice: {notice}",
            "=== SALES ATTENDANT GUIDANCE ===",
            "- The customer sent an image, but it does not appear to show an electronics product or could not be clearly identified.",
            "- Acknowledge their image naturally in 1 conversational sentence without using rigid opening formulas.",
            "- Politely ask them for the device brand/model name or invite them to send a clearer photo.",
            "- Do NOT make up products. Do NOT use dashes, asterisks (*), or bullet points. Speak naturally in the customer's active conversation language."
        ]
        return "\n".join(lines)

    product_name = vision_data.get("product_name", "Unknown Product")
    brand = vision_data.get("brand", "Unknown")
    category = vision_data.get("category", "Electronics")
    color = vision_data.get("color", "Standard")
    specs_summary = vision_data.get("specs_summary", "")
    search_query = vision_data.get("search_query", product_name)
    description = vision_data.get("description", "")

    lines = [
        "=== CUSTOMER PHOTO IDENTIFICATION (META VISION) ===",
        f"- Identified Product: {product_name}",
        f"- Brand: {brand}",
        f"- Category: {category}",
        f"- Visible Color / Finish: {color}",
        f"- Visual Specifications: {specs_summary}",
        f"- Catalog Search Query: {search_query}",
        f"- Visual Description: {description}",
        "=== STORE CATALOG MATCHES ==="
    ]

    normalized_matches = []
    if catalog_matches:
        for item in catalog_matches:
            if isinstance(item, tuple) and len(item) == 2:
                p, _ = item
                if isinstance(p, dict):
                    normalized_matches.append(p)
            elif isinstance(item, dict):
                normalized_matches.append(item)

    if normalized_matches:
        lines.append("Matches found in our store inventory:")
        for idx, p in enumerate(normalized_matches[:3], start=1):
            stock_str = "In Stock" if p.get("in_stock", True) else "Currently Out of Stock"
            price_str = p.get("price_formatted") or f"{p.get('price_bdt', '')} BDT"
            lines.append(
                f"{idx}. {p.get('name')}\n"
                f"   Price: {price_str}\n"
                f"   Availability: {stock_str}\n"
                f"   Specs: {p.get('specifications', 'N/A')}"
            )
        lines.extend([
            "=== SALES ATTENDANT GUIDANCE ===",
            f"- The customer sent a photo of {product_name}.",
            f"- Acknowledge their photo naturally in 1 conversational sentence, identifying the model as {product_name}.",
            "- Directly state its exact price and availability from our catalog match above.",
            "- Only mention stock if an item is out of stock. If in stock, state the exact price directly.",
            "- Answer any physical color or edition questions based on the photo and catalog details.",
            "- Speak naturally like a human sales attendant on Messenger. Do NOT use dashes (–, —), asterisks (*), bullet lists, or repetitive closing questions."
        ])
    else:
        lines.extend([
            f"- No exact match for '{product_name}' was found in our current store catalog.",
            "=== SALES ATTENDANT GUIDANCE ===",
            f"- The customer sent a photo of {product_name}.",
            f"- Acknowledge their photo naturally, mentioning {product_name}.",
            "- Politely inform them in 1 short sentence that we do not currently have this exact model in stock.",
            f"- If we carry other products from {brand} or in the {category} category, briefly suggest checking our available alternatives.",
            "- Do NOT use dashes, asterisks (*), bullet lists, or repetitive closing questions."
        ])

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print("=" * 60)
    print("TESTING META VISION SERVICE")
    print("=" * 60)

    # Test 1: Invalid URL error handling
    print("\n[Test 1] Error handling on invalid URL:")
    res_err = analyze_product_image("https://invalid-non-existent-domain-12345.com/fake.jpg")
    print("Result:", res_err)
    assert res_err["success"] is False
    assert "message" in res_err
    print("PASS: Error handled gracefully.")

    # Test 2: format_vision_context with failure
    print("\n[Test 2] format_vision_context with failure:")
    ctx_fail = format_vision_context(res_err)
    print("Context Preview:")
    print(ctx_fail[:150] + "...")
    assert "UNIDENTIFIED" in ctx_fail
    print("PASS: Unidentified format context generated.")

    # Test 3: format_vision_context with mock success and matches
    print("\n[Test 3] format_vision_context with successful match:")
    mock_success = {
        "success": True,
        "product_name": "MSI Stealth 14 Studio",
        "brand": "MSI",
        "category": "Laptops",
        "color": "Star Blue",
        "specs_summary": "14-inch OLED 120Hz, RTX 4060, i7-13700H",
        "search_query": "MSI Stealth 14",
        "description": "An MSI Stealth 14 Studio gaming laptop in Star Blue finish."
    }
    mock_catalog = [
        {
            "name": "MSI Stealth 14 Studio A13V",
            "price_formatted": "৳245,000",
            "price_bdt": 245000,
            "in_stock": True,
            "category": "Laptops",
            "specifications": "14-inch QHD+ 240Hz, Intel Core i7-13700H, 16GB DDR5, 1TB SSD, RTX 4060 8GB"
        }
    ]
    ctx_succ = format_vision_context(mock_success, mock_catalog)
    print("Context Preview:")
    print(ctx_succ[:250] + "...")
    assert "MSI Stealth 14 Studio" in ctx_succ
    assert "৳245,000" in ctx_succ
    print("PASS: Success format context generated.")

    # Test 4: Live image analysis with public Unsplash image if API key is present
    if getattr(config, "META_MODEL_API_KEY", ""):
        print("\n[Test 4] Live Meta Vision API Test:")
        test_img_url = "https://images.unsplash.com/photo-1517336714731-489689fd1ca8?w=500&q=80"
        live_res = analyze_product_image(test_img_url)
        print("Live Result:", json.dumps(live_res, indent=2))
        assert "success" in live_res
        if live_res["success"]:
            print(f"Product Identified: {live_res['product_name']} ({live_res['brand']})")
            print(f"Search Query: {live_res['search_query']}")
            print("PASS: Live image correctly analyzed!")
        else:
            print("Live result returned unsuccessful:", live_res["message"])
    else:
        print("\n[Test 4] Skipped Live test (no META_MODEL_API_KEY configured).")

    print("\n" + "=" * 60)
    print("ALL TESTS COMPLETED SUCCESSFULLY!")
    print("=" * 60)
