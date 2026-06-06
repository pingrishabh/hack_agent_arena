"""Offline API-retrieval index (ARCH.md §4 semantic layer; the #1 token saver).

The 457 AppWorld API docs are available via local Python calls that cost ZERO
LLM tokens. We build a compact catalog once, cache it to disk, and at plan time
inject only the top-k relevant API *signatures* into the prompt — instead of
letting the agent dump huge `api_docs` output into the context every task (which
then gets re-sent every turn). This removes the single biggest token sink.

Zero external dependencies: a small pure-Python BM25 (we've had enough dependency
conflicts).
"""

from __future__ import annotations

import json
import math
import os
import re

CATALOG_PATH = os.path.join(os.path.dirname(__file__), ".api_catalog.json")
_TOKEN_RE = re.compile(r"[a-z0-9_]+")
# api_docs is excluded on purpose — we replace runtime discovery with this index.
_SKIP_APPS = {"api_docs"}


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _signature(doc: dict) -> str:
    """Compact, ~30-60 token signature for one API."""
    params = []
    for p in doc.get("parameters", []) or []:
        nm, ty = p.get("name", "?"), p.get("type", "")
        params.append(f"{nm}:{ty}" + ("" if p.get("required") else "?"))
    desc = (doc.get("description", "") or "").strip().splitlines()
    desc = desc[0][:120] if desc else ""
    return f"{doc.get('app_name')}.{doc.get('api_name')}({', '.join(params)}) — {desc}"


def build_catalog(apis) -> list[dict]:
    """Walk every app/API locally (no LLM) and return [{app, api, sig, text}]."""
    catalog: list[dict] = []
    for app in apis.api_docs.show_app_descriptions():
        app_name = app["name"] if isinstance(app, dict) else app
        app_desc = app.get("description", "") if isinstance(app, dict) else ""
        if app_name in _SKIP_APPS:
            continue
        try:
            api_list = apis.api_docs.show_api_descriptions(app_name=app_name)
        except Exception:
            continue
        for api in api_list:
            api_name = api["name"] if isinstance(api, dict) else api
            try:
                doc = apis.api_docs.show_api_doc(app_name=app_name, api_name=api_name)
            except Exception:
                continue
            sig = _signature(doc)
            # index text = app (x2 weight) + app description + api + description + param names.
            # The repeated app_name + app description biases routing to the right app.
            pnames = " ".join(p.get("name", "") for p in doc.get("parameters", []) or [])
            text = f"{app_name} {app_name} {app_desc} {api_name} {doc.get('description','')} {pnames}"
            catalog.append({"app": app_name, "api": api_name, "sig": sig, "text": text})
    return catalog


class ApiIndex:
    """Tiny Okapi-BM25 index over API catalog entries."""

    def __init__(self, entries: list[dict], k1: float = 1.5, b: float = 0.75):
        self.entries = entries
        self.k1, self.b = k1, b
        self.docs = [_tokens(e["text"]) for e in entries]
        self.dl = [len(d) for d in self.docs]
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.docs else 0.0
        self.df: dict[str, int] = {}
        for d in self.docs:
            for w in set(d):
                self.df[w] = self.df.get(w, 0) + 1
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in self.df.items()}
        self.tf = [{} for _ in self.docs]
        for i, d in enumerate(self.docs):
            for w in d:
                self.tf[i][w] = self.tf[i].get(w, 0) + 1

    def retrieve(self, query: str, k: int = 8) -> list[str]:
        if not self.entries:
            return []
        q = _tokens(query)
        scores = []
        for i in range(len(self.docs)):
            s = 0.0
            dl = self.dl[i] or 1
            for w in q:
                if w not in self.tf[i]:
                    continue
                f = self.tf[i][w]
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += self.idf.get(w, 0.0) * (f * (self.k1 + 1)) / denom
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [self.entries[i]["sig"] for _, i in scores[:k]]


def _sig_key(sig: str) -> str:
    """Normalize a signature to 'app.api' for dedup across retrievers."""
    return (sig.split("(", 1)[0] or sig).strip().lower()


def _rrf(ranked_lists: list[list[str]], k: int, c: int = 60) -> list[str]:
    """Reciprocal-rank fusion of multiple ranked signature lists."""
    scores: dict[str, float] = {}
    repr_sig: dict[str, str] = {}
    for lst in ranked_lists:
        for rank, sig in enumerate(lst):
            key = _sig_key(sig)
            scores[key] = scores.get(key, 0.0) + 1.0 / (c + rank)
            repr_sig.setdefault(key, sig)
    ordered = sorted(scores, key=lambda key: scores[key], reverse=True)
    return [repr_sig[key] for key in ordered[:k]]


class ApiRetriever:
    """Hybrid API retrieval: BM25 (local, always-on floor) + optional HydraDB
    semantic. Modes: 'bm25' | 'hydra' | 'hybrid'. Any HydraDB failure or empty
    result silently falls back to BM25 — the token-critical path never goes dark.
    """

    def __init__(self, bm25: ApiIndex, hydra_query=None, mode: str = "hybrid"):
        self.bm25 = bm25
        self.hydra_query = hydra_query
        self.mode = mode if mode in ("bm25", "hydra", "hybrid") else "hybrid"

    def _safe_hydra(self, query: str, k: int) -> list[str]:
        if not self.hydra_query:
            return []
        try:
            return self.hydra_query(query, k) or []
        except Exception:
            return []

    def retrieve(self, query: str, k: int = 12) -> list[str]:
        bm = self.bm25.retrieve(query, k)
        if self.mode == "bm25" or not self.hydra_query:
            return bm
        hs = self._safe_hydra(query, k)
        if not hs:
            return bm  # floor / fallback
        if self.mode == "hydra":
            return hs[:k]
        return _rrf([hs, bm], k)  # hybrid


_INDEX: ApiIndex | None = None


def get_index(apis=None) -> ApiIndex:
    """Load the index from disk; build+cache it from `apis` on first run."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    catalog: list[dict] = []
    if os.path.exists(CATALOG_PATH):
        try:
            with open(CATALOG_PATH) as f:
                catalog = json.load(f)
        except Exception:
            catalog = []
    if not catalog and apis is not None:
        catalog = build_catalog(apis)
        try:
            with open(CATALOG_PATH, "w") as f:
                json.dump(catalog, f)
        except Exception:
            pass
    _INDEX = ApiIndex(catalog)
    return _INDEX
