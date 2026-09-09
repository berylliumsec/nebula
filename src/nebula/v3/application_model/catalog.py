"""Canonical, evidence-neutral web application vocabulary.

The catalog describes what agents and operators may model.  Importing it never
creates graph objects and none of its examples are assertions about a project.
"""

from __future__ import annotations

from .registry import (
    CategoryDefinition,
    PropertyDefinition,
    RelationshipDefinition,
    SchemaRegistry,
    TypeDefinition,
)


_CATEGORIES = {
    "Structure": "Application structure, navigation, inputs, and static resources.",
    "APIs": "Interfaces, operations, protocols, and exchanged data.",
    "Identity": "Users, access rights, authentication, and login state.",
    "Security": "Protections, their scope, and evidence of enforcement.",
    "Infrastructure": "Hosting, routing, and content delivery.",
    "Dependencies": "Software dependencies, integrations, and data storage.",
    "Client execution": "Browser code, behavior, and execution relationships.",
    "Browser storage": "Browser-held state and its observed uses.",
    "Backend processing": "Server-side processing supported by evidence.",
    "Data": "Identifiable data resources and operations without unseen internals.",
}

_TYPES = {
    "Structure": (
        "Application",
        "Site",
        "Page",
        "SiteLink",
        "Route",
        "Form",
        "Input",
        "Parameter",
        "Button",
        "Asset",
        "Upload",
        "Download",
    ),
    "APIs": (
        "API",
        "Endpoint",
        "Operation",
        "Request",
        "Response",
        "Header",
        "Schema",
        "GraphQLOperation",
        "WebSocketChannel",
        "EventStream",
        "Webhook",
    ),
    "Identity": (
        "User",
        "Role",
        "Permission",
        "AuthenticationFlow",
        "LoginMethod",
        "Session",
        "IdentityProvider",
        "Token",
    ),
    "Security": (
        "SecurityPolicy",
        "AccessControl",
        "CSRFToken",
        "RateLimit",
        "Certificate",
        "Firewall",
        "WAF",
        "BotProtection",
    ),
    "Infrastructure": (
        "Domain",
        "Subdomain",
        "Origin",
        "Service",
        "Server",
        "Proxy",
        "ReverseProxy",
        "LoadBalancer",
        "Gateway",
        "CDN",
    ),
    "Dependencies": (
        "Library",
        "Framework",
        "ExternalService",
        "Storage",
        "Database",
        "Datastore",
        "Cache",
        "ObjectStorage",
        "SearchIndex",
    ),
    "Client execution": (
        "JavaScriptAsset",
        "ScriptModule",
        "ClientComponent",
        "EventHandler",
        "ServiceWorker",
        "WebWorker",
    ),
    "Browser storage": (
        "Cookie",
        "LocalStorage",
        "SessionStorage",
        "IndexedDB",
        "BrowserCache",
    ),
    "Backend processing": ("Middleware", "BackgroundJob", "Queue", "ScheduledTask"),
    "Data": ("Collection", "Table", "QueryOperation", "File"),
}

_PARENTS = {
    "JavaScriptAsset": "Asset",
    "Database": "Storage",
    "QueryOperation": "Operation",
}

_DESCRIPTIONS = {
    "Application": "A logical web application assembled from one or more sites or services.",
    "Site": "A browsable web presence observed at one or more origins.",
    "Page": "A user-visible document or client-rendered view.",
    "SiteLink": "A navigation link exposed by a site or page.",
    "Route": "A navigable application path or route pattern.",
    "Form": "A set of controls that submits operator or browser input.",
    "Input": "A form or component control that accepts input.",
    "Parameter": "A named input exchanged through a route, operation, or request.",
    "Button": "An interactive control that initiates browser behavior.",
    "Asset": "A static or generated resource addressable by the application.",
    "Upload": "A browser-to-application file transfer interaction.",
    "Download": "An application-to-browser file transfer interaction.",
    "API": "A cohesive application programming interface.",
    "Endpoint": "A network-reachable interface location.",
    "Operation": "An invocable action exposed by an interface.",
    "Request": "An observed outbound protocol message.",
    "Response": "An observed inbound protocol message.",
    "Header": "A named request or response metadata field.",
    "Schema": "A declared or inferred shape for exchanged data.",
    "GraphQLOperation": "A GraphQL query, mutation, or subscription operation.",
    "WebSocketChannel": "A bidirectional WebSocket communication channel.",
    "EventStream": "A unidirectional stream of server-originated events.",
    "Webhook": "An endpoint intended to receive event callbacks.",
    "User": "A represented human or machine user identity.",
    "Role": "A named grouping of access responsibilities.",
    "Permission": "A named authorization capability.",
    "AuthenticationFlow": "A sequence through which identity is established.",
    "LoginMethod": "A user-selectable way to authenticate.",
    "Session": "An application login or continuity context, distinct from browser capture.",
    "IdentityProvider": "A service that establishes or vouches for identity.",
    "Token": "Metadata identifying a security token without retaining its secret value.",
    "SecurityPolicy": "A declared or inferred rule governing application behavior.",
    "AccessControl": "An authorization control applied to a resource or operation.",
    "CSRFToken": "Metadata about an anti-CSRF token, excluding the token secret.",
    "RateLimit": "A restriction on operation frequency or volume.",
    "Certificate": "Public certificate identity and validity metadata.",
    "Firewall": "A hypothesized or observed network traffic control boundary.",
    "WAF": "A hypothesized or observed web application firewall control.",
    "BotProtection": "A control intended to detect or restrict automated clients.",
    "Domain": "A registrable or fully qualified DNS domain.",
    "Subdomain": "A DNS name subordinate to another modeled domain.",
    "Origin": "A scheme, host, and port security origin.",
    "Service": "A network-accessible application service.",
    "Server": "A serving host or runtime supported by evidence.",
    "Proxy": "An intermediary forwarding traffic between parties.",
    "ReverseProxy": "An intermediary forwarding requests toward application services.",
    "LoadBalancer": "An intermediary distributing traffic among targets.",
    "Gateway": "A boundary routing or mediating access to services.",
    "CDN": "A content delivery network serving application resources.",
    "Library": "A software library used by an application.",
    "Framework": "A software framework shaping application behavior.",
    "ExternalService": "A service operated outside the modeled application boundary.",
    "Storage": "A data persistence capability without assuming its implementation.",
    "Database": "A database evidenced by the application, without inventing topology.",
    "Datastore": "A non-specific structured persistence system.",
    "Cache": "A temporary data storage or delivery layer.",
    "ObjectStorage": "A service storing addressable data objects.",
    "SearchIndex": "An indexed data resource used for retrieval.",
    "JavaScriptAsset": "An Asset containing browser-executable JavaScript.",
    "ScriptModule": "A browser-executable module loaded by another script or page.",
    "ClientComponent": "An identifiable client-side user-interface component.",
    "EventHandler": "Client behavior associated with an event.",
    "ServiceWorker": "A browser service worker registration or script.",
    "WebWorker": "A browser worker executing outside the document main thread.",
    "Cookie": "Cookie metadata and observed use, excluding the cookie value.",
    "LocalStorage": "A named localStorage entry or namespace, excluding secret values.",
    "SessionStorage": "A named sessionStorage entry or namespace, excluding secret values.",
    "IndexedDB": "An observed browser IndexedDB database.",
    "BrowserCache": "A browser-controlled cache or Cache API namespace.",
    "Middleware": "A server-side processing stage supported by evidence.",
    "BackgroundJob": "A server-side job executed outside the immediate request path.",
    "Queue": "A queue used to exchange or defer work.",
    "ScheduledTask": "A server-side task with an evidenced schedule or trigger.",
    "Collection": "A named grouping of data records or documents.",
    "Table": "A named tabular data resource supported by evidence.",
    "QueryOperation": "An Operation that retrieves or changes data through a query.",
    "File": "An identifiable application data file, distinct from a static Asset.",
}

_URL_TYPES = frozenset(
    {
        "Site",
        "Page",
        "SiteLink",
        "Route",
        "Asset",
        "Upload",
        "Download",
        "API",
        "Endpoint",
        "Webhook",
        "Origin",
        "Service",
        "ExternalService",
        "JavaScriptAsset",
        "ServiceWorker",
        "WebWorker",
    }
)
_NAMED_TYPES = (
    frozenset(name for names in _TYPES.values() for name in names) - _URL_TYPES
)


def _properties(name: str) -> tuple[PropertyDefinition, ...]:
    if name in _PARENTS:
        return ()
    if name in _URL_TYPES:
        return (
            PropertyDefinition(name="url", description="Observed or declared URL."),
            PropertyDefinition(
                name="display_name", description="Human-readable label when known."
            ),
        )
    properties = [
        PropertyDefinition(
            name="name", description="Observed or declared identifying name."
        ),
        PropertyDefinition(
            name="description", description="Non-secret descriptive metadata."
        ),
    ]
    if name in {"Request", "Response"}:
        properties.append(
            PropertyDefinition(
                name="status_code",
                kind="integer",
                description="Observed HTTP status code when applicable.",
            )
        )
    if name in {"Operation", "GraphQLOperation"}:
        properties.append(
            PropertyDefinition(
                name="method", description="Observed protocol method or operation kind."
            )
        )
    if name == "Cookie":
        properties.extend(
            (
                PropertyDefinition(
                    name="domain", description="Cookie Domain attribute."
                ),
                PropertyDefinition(name="path", description="Cookie Path attribute."),
                PropertyDefinition(
                    name="secure",
                    kind="boolean",
                    description="Whether the Secure attribute was observed.",
                ),
                PropertyDefinition(
                    name="http_only",
                    kind="boolean",
                    description="Whether the HttpOnly attribute was observed.",
                ),
                PropertyDefinition(
                    name="same_site",
                    kind="enum",
                    description="Observed SameSite policy.",
                    choices=("strict", "lax", "none"),
                ),
            )
        )
    if name in {"Token", "CSRFToken"}:
        properties.append(
            PropertyDefinition(
                name="fingerprint",
                description="Non-reversible identifier for correlation; never the token value.",
            )
        )
    return tuple(properties)


def _type(name: str, category: str) -> TypeDefinition:
    parent = _PARENTS.get(name)
    hint = "url" if (name in _URL_TYPES or parent == "Asset") else "name"
    return TypeDefinition(
        name=name,
        label=name,
        category=category,
        description=_DESCRIPTIONS[name],
        extends=parent,
        properties=_properties(name),
        identity_hints=(hint,),
        evidence_examples=(
            f"Browser capture, source artifact, or operator record identifying this {name}.",
        ),
    )


def _relation(
    name: str,
    label: str,
    description: str,
    sources: tuple[str, ...],
    targets: tuple[str, ...],
) -> RelationshipDefinition:
    return RelationshipDefinition(
        name=name,
        label=label,
        description=description,
        source_types=sources,
        target_types=targets,
        evidence_examples=(
            "Recorded browser exchange, page behavior, source artifact, or operator-supplied evidence.",
        ),
    )


_RELATIONSHIPS = (
    _relation(
        "contains",
        "Contains",
        "The source structurally contains the target.",
        (
            "Application",
            "Site",
            "Page",
            "Form",
            "API",
            "Schema",
            "Database",
            "Collection",
        ),
        ("*",),
    ),
    _relation(
        "links_to",
        "Links to",
        "The source exposes navigation to the target.",
        ("Page", "SiteLink", "Button"),
        ("Page", "Route", "Site", "Origin"),
    ),
    _relation(
        "submits_to",
        "Submits to",
        "The source submits data to the target.",
        ("Form", "Upload"),
        ("Endpoint", "Operation", "Route"),
    ),
    _relation(
        "accepts_input",
        "Accepts input",
        "The source accepts the target input or parameter.",
        ("Form", "Operation", "Request", "Endpoint"),
        ("Input", "Parameter", "File"),
    ),
    _relation(
        "loads",
        "Loads",
        "The source loads the target resource or code.",
        ("Page", "JavaScriptAsset", "ScriptModule", "ServiceWorker"),
        ("Asset", "ScriptModule", "WebWorker"),
    ),
    _relation(
        "executes",
        "Executes",
        "The source executes the target behavior.",
        (
            "Page",
            "JavaScriptAsset",
            "ScriptModule",
            "ClientComponent",
            "EventHandler",
            "ScheduledTask",
            "Queue",
        ),
        ("Operation", "EventHandler", "BackgroundJob", "ScriptModule"),
    ),
    _relation(
        "calls",
        "Calls",
        "The source invokes the target interface or operation.",
        ("*",),
        (
            "API",
            "Endpoint",
            "Operation",
            "GraphQLOperation",
            "Webhook",
            "ExternalService",
        ),
    ),
    _relation(
        "authenticates_via",
        "Authenticates via",
        "The source establishes identity using the target.",
        ("User", "Application", "Site", "Session"),
        ("AuthenticationFlow", "LoginMethod", "IdentityProvider", "Token"),
    ),
    _relation(
        "requires_permission",
        "Requires permission",
        "Use of the source requires the target permission or role.",
        ("*",),
        ("Permission", "Role"),
    ),
    _relation(
        "uses_cookie",
        "Uses cookie",
        "The source uses cookie metadata for an evidenced purpose.",
        ("*",),
        ("Cookie",),
    ),
    _relation(
        "governed_by",
        "Governed by",
        "The source is governed by the target policy or control.",
        ("*",),
        ("SecurityPolicy", "AccessControl", "RateLimit"),
    ),
    _relation(
        "stores_in",
        "Stores in",
        "The source stores data in the target.",
        ("Application", "Service", "Operation", "BackgroundJob"),
        ("Storage", "BrowserCache", "LocalStorage", "SessionStorage", "IndexedDB"),
    ),
    _relation(
        "reads_from",
        "Reads from",
        "The source reads data from the target.",
        ("Operation", "QueryOperation", "BackgroundJob", "ClientComponent"),
        ("Storage", "Collection", "Table", "File", "SearchIndex"),
    ),
    _relation(
        "writes_to",
        "Writes to",
        "The source writes data to the target.",
        ("Operation", "QueryOperation", "BackgroundJob", "Upload"),
        ("Storage", "Collection", "Table", "File", "SearchIndex"),
    ),
    _relation(
        "queries",
        "Queries",
        "The source queries the target data resource.",
        ("QueryOperation", "Operation", "SearchIndex"),
        ("Database", "Datastore", "Collection", "Table", "SearchIndex"),
    ),
    _relation(
        "served_by",
        "Served by",
        "The source is served or delivered by the target.",
        ("Site", "Page", "Asset", "API", "Endpoint", "Origin"),
        (
            "Service",
            "Server",
            "Proxy",
            "ReverseProxy",
            "LoadBalancer",
            "Gateway",
            "CDN",
        ),
    ),
    _relation(
        "depends_on",
        "Depends on",
        "The source depends on the target capability.",
        ("*",),
        ("Library", "Framework", "ExternalService", "Storage", "Service"),
    ),
    _relation(
        "protected_by",
        "Protected by",
        "The source is protected by the target control; the relationship may remain hypothesized.",
        ("*",),
        ("Firewall", "WAF", "BotProtection", "AccessControl", "CSRFToken", "RateLimit"),
    ),
)


def builtin_registry() -> SchemaRegistry:
    """Return a fresh immutable snapshot of Nebula's built-in web schema."""

    categories = tuple(
        CategoryDefinition(name=name, purpose=purpose)
        for name, purpose in _CATEGORIES.items()
    )
    types = tuple(
        _type(name, category) for category, names in _TYPES.items() for name in names
    )
    return SchemaRegistry(categories, types, _RELATIONSHIPS)
