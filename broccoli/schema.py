"""Schema and document validation (SystemDesign.md §2).

Validation happens here because the schema is a trust boundary: everything
downstream (the inverted index, the ANN graph, the bitmaps) assumes well-formed,
correctly-typed input.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class FieldType:
    """Base for all schema fields."""

    kind = "unknown"

    def spec(self) -> Dict[str, Any]:
        return {"kind": self.kind}

    def validate(self, name: str, value: Any) -> Any:
        return value


@dataclass
class Text(FieldType):
    """Analyzed text → lexical (inverted) index."""

    analyzer: str = "standard"
    stored: bool = True
    kind: str = field(default="text", init=False, repr=False)

    def spec(self) -> Dict[str, Any]:
        return {"kind": "text", "analyzer": self.analyzer, "stored": self.stored}

    def validate(self, name: str, value: Any) -> Any:
        if not isinstance(value, str):
            raise TypeError(f"field '{name}' expects str, got {type(value).__name__}")
        return value


@dataclass
class Vector(FieldType):
    """Dense embedding → ANN index."""

    dim: int = 0
    metric: str = "cosine"  # cosine | l2 | ip
    m: int = 16             # HNSW graph connectivity
    ef_construction: int = 200
    kind: str = field(default="vector", init=False, repr=False)

    def __post_init__(self) -> None:
        if self.dim <= 0:
            raise ValueError("Vector(dim=...) must be a positive integer")
        if self.metric not in ("cosine", "l2", "ip"):
            raise ValueError(f"unsupported metric '{self.metric}'")

    def spec(self) -> Dict[str, Any]:
        return {"kind": "vector", "dim": self.dim, "metric": self.metric,
                "m": self.m, "ef_construction": self.ef_construction}

    def validate(self, name: str, value: Any) -> Any:
        vec = list(value)
        if len(vec) != self.dim:
            raise ValueError(
                f"field '{name}' expects dim {self.dim}, got {len(vec)}")
        try:
            return [float(x) for x in vec]
        except (TypeError, ValueError):
            raise TypeError(f"field '{name}' must contain numbers")


@dataclass
class Keyword(FieldType):
    """Exact-match string → bitmap index."""

    kind: str = field(default="keyword", init=False, repr=False)

    def validate(self, name: str, value: Any) -> Any:
        if not isinstance(value, str):
            raise TypeError(f"field '{name}' expects str, got {type(value).__name__}")
        return value


@dataclass
class Int(FieldType):
    kind: str = field(default="int", init=False, repr=False)

    def validate(self, name: str, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"field '{name}' expects int")
        return value


@dataclass
class Float(FieldType):
    kind: str = field(default="float", init=False, repr=False)

    def validate(self, name: str, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"field '{name}' expects a number")
        return float(value)


@dataclass
class Bool(FieldType):
    kind: str = field(default="bool", init=False, repr=False)

    def validate(self, name: str, value: Any) -> Any:
        if not isinstance(value, bool):
            raise TypeError(f"field '{name}' expects bool")
        return value


@dataclass
class Datetime(FieldType):
    """Stored internally as a POSIX timestamp so temporal filters are numeric."""

    kind: str = field(default="datetime", init=False, repr=False)

    def validate(self, name: str, value: Any) -> Any:
        return to_timestamp(value, name)


def to_timestamp(value: Any, name: str = "value") -> float:
    """Accept datetime/date, epoch number, or ISO-8601 string."""
    if isinstance(value, _dt.datetime):
        return value.timestamp()
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day).timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return _dt.datetime.fromisoformat(value).timestamp()
        except ValueError:
            raise ValueError(f"field '{name}': cannot parse datetime '{value}'")
    raise TypeError(f"field '{name}' expects a datetime, ISO string, or epoch number")


_KIND_TO_CLS = {
    "text": Text, "vector": Vector, "keyword": Keyword,
    "int": Int, "float": Float, "bool": Bool, "datetime": Datetime,
}

NUMERIC_KINDS = ("int", "float", "datetime")
STRUCTURED_KINDS = ("keyword", "int", "float", "bool", "datetime")


class Schema:
    """Declares field types and routes each field to the right index."""

    def __init__(self, fields: Dict[str, FieldType]):
        if not fields:
            raise ValueError("schema must declare at least one field")
        for name, f in fields.items():
            if not isinstance(f, FieldType):
                raise TypeError(f"field '{name}' must be a broccoli field type")
        self.fields = dict(fields)

    # -- routing helpers used by the engine -------------------------------- #
    def names_of(self, *kinds: str) -> List[str]:
        return [n for n, f in self.fields.items() if f.kind in kinds]

    @property
    def text_fields(self) -> List[str]:
        return self.names_of("text")

    @property
    def vector_fields(self) -> List[str]:
        return self.names_of("vector")

    @property
    def structured_fields(self) -> List[str]:
        return self.names_of(*STRUCTURED_KINDS)

    def vector_field(self, name: Optional[str] = None) -> Optional[str]:
        vfs = self.vector_fields
        if name:
            if name not in vfs:
                raise KeyError(f"'{name}' is not a vector field")
            return name
        return vfs[0] if vfs else None

    def validate(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Trust boundary: reject unknown/mistyped fields before indexing."""
        if "id" not in doc:
            raise ValueError("document must have an 'id'")
        out: Dict[str, Any] = {"id": str(doc["id"])}
        for key, value in doc.items():
            if key == "id":
                continue
            if key not in self.fields:
                raise KeyError(f"unknown field '{key}' (not in schema)")
            if value is None:
                continue
            out[key] = self.fields[key].validate(key, value)
        return out

    # -- persistence -------------------------------------------------------- #
    def spec(self) -> Dict[str, Any]:
        return {n: f.spec() for n, f in self.fields.items()}

    @classmethod
    def from_spec(cls, spec: Dict[str, Any]) -> "Schema":
        fields: Dict[str, FieldType] = {}
        for name, s in spec.items():
            kind = s["kind"]
            klass = _KIND_TO_CLS[kind]
            if kind == "text":
                fields[name] = Text(analyzer=s.get("analyzer", "standard"),
                                    stored=s.get("stored", True))
            elif kind == "vector":
                fields[name] = Vector(dim=s["dim"], metric=s.get("metric", "cosine"),
                                      m=s.get("m", 16),
                                      ef_construction=s.get("ef_construction", 200))
            else:
                fields[name] = klass()
        return cls(fields)
