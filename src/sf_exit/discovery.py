"""Decide which objects and fields to export."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

# Objects that can be described and "queried" but are really metadata views,
# require a mandatory WHERE filter, or are virtual roll-ups of data exported elsewhere.
NOT_EXPORTABLE = {
    "AggregateResult": "virtual object",
    "ActivityHistory": "virtual roll-up of Task/Event (exported directly)",
    "AppTabMember": "metadata view",
    "AttachedContentDocument": "virtual roll-up of ContentDocumentLink",
    "AttachedContentNote": "virtual roll-up of ContentDocumentLink",
    "ColorDefinition": "metadata view",
    "CombinedAttachment": "virtual roll-up of Attachment/ContentDocumentLink",
    "ContentFolderItem": "requires a filter",
    "ContentFolderMember": "requires a filter",
    "ContentHubItem": "external content",
    "DataStatistics": "requires a filter",
    "DataType": "metadata view",
    "DatacloudAddress": "external data",
    "EmailStatus": "virtual roll-up",
    "EntityDefinition": "metadata view",
    "EntityParticle": "metadata view",
    "FeedLike": "requires a filter by Id",
    "FeedTrackedChange": "requires a filter by Id (changes are in the *History objects)",
    "NewsFeed": "requires a filter (personal feed view)",
    "UserProfileFeed": "requires a WITH UserId clause (personal feed view)",
    "ApexTypeImplementor": "requires a filter (metadata view)",
    "FlowVariableView": "requires a filter (metadata view)",
    "FlowVersionView": "requires a filter (metadata view)",
    "RelatedListDefinition": "requires a filter (metadata view)",
    "RelatedListColumnDefinition": "requires a filter (metadata view)",
    "SiteDetail": "requires a filter",
    "NetworkUserHistoryRecent": "requires a filter",
    "PlatformEventUsageMetric": "requires a filter",
    "DatacloudCompany": "external Data.com data",
    "DatacloudContact": "external Data.com data",
    "DatacloudDandBCompany": "external Data.com data",
    "DatacloudOwnedEntity": "external Data.com data",
    "DatacloudPurchaseUsage": "external Data.com data",
    "DcSocialProfile": "external data",
    "DcSocialProfileHandle": "external data",
    "FormulaFunction": "metadata view",
    "FormulaFunctionAllowedType": "metadata view",
    "FormulaFunctionCategory": "metadata view",
    "FormulaOperator": "metadata view",
    "FieldDefinition": "metadata view",
    "FieldSecurityClassification": "metadata view",
    "FlexQueueItem": "transient Apex queue",
    "IconDefinition": "metadata view",
    "IdeaComment": "requires a filter",
    "ListViewChartInstance": "requires a filter",
    "LookedUpFromActivity": "virtual roll-up",
    "Name": "virtual object",
    "NoteAndAttachment": "virtual roll-up of Note/Attachment",
    "OpenActivity": "virtual roll-up of Task/Event (exported directly)",
    "OutgoingEmail": "requires a filter",
    "OutgoingEmailRelation": "requires a filter",
    "OwnedContentDocument": "virtual roll-up",
    "OwnerChangeOptionInfo": "metadata view",
    "PicklistValueInfo": "metadata view",
    "PlatformAction": "metadata view",
    "ProcessInstanceHistory": "virtual roll-up of ProcessInstanceStep",
    "Publisher": "metadata view",
    "RelationshipDomain": "metadata view",
    "RelationshipInfo": "metadata view",
    "SearchLayout": "metadata view",
    "ServiceFieldDataType": "metadata view",
    "UserEntityAccess": "metadata view",
    "UserFieldAccess": "metadata view",
    "UserRecordAccess": "requires a filter",
    "Vote": "requires a filter",
}

# Exported in a dedicated pass because Salesforce refuses unfiltered queries on it.
SPECIAL_OBJECTS = {"ContentDocumentLink"}

# Field types the Bulk API cannot return. Compound fields are still exported via their
# component fields (BillingStreet, BillingCity, ... / Location__Latitude__s, ...).
COMPOUND_TYPES = {"address", "location"}
BLOB_TYPES = {"base64"}


@dataclass
class ObjectPlan:
    name: str
    label: str
    custom: bool
    fields: List[str] = field(default_factory=list)
    field_types: Dict[str, str] = field(default_factory=dict)
    approx_rows: Optional[int] = None
    describe: Dict[str, Any] = field(default_factory=dict)
    # Objects Salesforce only returns with a mandatory filter are exported as the union of
    # several filtered queries (e.g. Knowledge articles: one query per PublishStatus).
    where_variants: List[str] = field(default_factory=list)

    def soql(self, where: str = "") -> str:
        q = f"SELECT {', '.join(self.fields)} FROM {self.name}"
        return q + (f" WHERE {where}" if where else "")


def _matches(name: str, patterns: Sequence[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


def select_objects(
    global_describe: Iterable[Dict[str, Any]],
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Return (objects_to_export, skipped{name: reason})."""
    chosen: List[Dict[str, Any]] = []
    skipped: Dict[str, str] = {}
    for obj in global_describe:
        name = obj["name"]
        if include and not _matches(name, include):
            continue
        if exclude and _matches(name, exclude):
            skipped[name] = "excluded by --exclude"
            continue
        if not obj.get("queryable", False):
            continue
        if obj.get("deprecatedAndHidden"):
            continue
        if name in SPECIAL_OBJECTS:
            continue
        if name in NOT_EXPORTABLE:
            skipped[name] = NOT_EXPORTABLE[name]
            continue
        if name.endswith("ChangeEvent") or name.endswith("__e"):
            continue  # streaming events, not stored data
        if name.endswith("__x"):
            skipped[name] = "external object (data lives outside Salesforce)"
            continue
        if name.endswith("__b"):
            skipped[name] = "big object (export with Async SOQL / Bulk API by index)"
            continue
        chosen.append(obj)
    chosen.sort(key=lambda o: o["name"].lower())
    return chosen, skipped


def plan_object(describe: Dict[str, Any]) -> ObjectPlan:
    fields: List[str] = []
    types: Dict[str, str] = {}
    for f in describe.get("fields", []):
        ftype = f.get("type", "string")
        if ftype in COMPOUND_TYPES or ftype in BLOB_TYPES:
            continue
        if f.get("deprecatedAndHidden"):
            continue
        fields.append(f["name"])
        types[f["name"]] = ftype
    if "Id" in fields:  # keep Id first: friendlier CSVs and primary keys
        fields.remove("Id")
        fields.insert(0, "Id")
    name = describe["name"]
    variants: List[str] = []
    if (name == "KnowledgeArticleVersion" or name.endswith("__kav")) and "PublishStatus" in types:
        variants = [f"PublishStatus = '{s}'" for s in ("Online", "Draft", "Archived")]
    return ObjectPlan(
        name=name,
        label=describe.get("label", name),
        custom=bool(describe.get("custom")),
        fields=fields,
        field_types=types,
        describe=describe,
        where_variants=variants,
    )


# Errors meaning "Salesforce will never return this object as a plain bulk export", as
# opposed to transient or permission problems. Such objects are reported as skipped.
_NOT_EXPORTABLE_HINTS = (
    "implementation restriction",
    "requires a filter",
    "must filter",
    "with userid",
    "not supported by the bulk api",  # only meaningful once REST has also failed
    "invalid_type_for_operation",
    "functionality_not_enabled",
    "not supported",
    "external object",
    "big object",
    "when retrieving results with metadata or fullname fields",
    "invalid_type",
)


def is_not_exportable(message: str) -> bool:
    m = message.lower()
    if "insufficient_access" in m or "request_limit_exceeded" in m or "invalid_session" in m:
        return False
    return any(h in m for h in _NOT_EXPORTABLE_HINTS)
