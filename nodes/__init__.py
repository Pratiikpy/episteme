"""Node registry — assembles all Episteme endpoints and their independent verifiers."""
from __future__ import annotations

from runtime import NodeRegistry
from nodes.core_utils import (
    ArtifactVerifyNode,
    FileInspectNode,
    FileInspectVerifier,
    HashComputeNode,
    TextDiffNode,
    UnitConvertNode,
)
from nodes.data_nodes import (
    CsvProfileNode,
    CsvProfileVerifier,
    DataDiffNode,
    DataQuerySqlNode,
)
from nodes.web_nodes import UrlInspectNode, UrlToMarkdownNode
from nodes.doc_nodes import DocumentToMarkdownNode
from nodes.api_data_nodes import (
    DataTransformJsonNode,
    DocumentChunkNode,
    OpenApiInspectNode,
)
from nodes.image_nodes import ImageInspectNode, ImageTransformNode
from nodes.pdf_nodes import PdfManipulateNode, DocumentCompareNode
from nodes.data2_nodes import (
    ChartSpecNode,
    DataConvertNode,
    DataDedupeNode,
    DataStatsNode,
    DataValidateNode,
)
from nodes.api2_nodes import OpenApiDiffNode, OpenApiLintNode, SchemaGenerateNode
from nodes.web2_nodes import PageLinksNode, PageExtractNode
from nodes.code_nodes import RepoMapNode, RepoLintNode, RepoScanSecretsNode
from nodes.sim_nodes import SimRunNode
from nodes.more2_nodes import (
    ApiToMcpNode,
    DataCleanNode,
    DataJoinNode,
    DocumentRedactPiiNode,
    EmailValidateNode,
    McpValidateNode,
    ObjectDiffNode,
    SiteMapNode,
)
from nodes.util2_nodes import CsvToTableNode, ReceiptVerifyNode, TextStatsNode
from nodes.final_nodes import DataPivotNode, RobotsCheckNode
from workflow import WorkflowComposeNode

# LLM nodes are optional (require openai lib + key); import defensively.
try:
    from nodes.llm_nodes import DocumentExtractJsonNode, TextSummarizeNode  # type: ignore
    _HAS_LLM = True
except Exception:  # noqa
    _HAS_LLM = False


def build_registry() -> NodeRegistry:
    reg = NodeRegistry()
    # deterministic core (with differential verifiers → L4-capable)
    reg.register(FileInspectNode(), verifier="file.inspect.verify")
    reg.register(FileInspectVerifier())
    reg.register(HashComputeNode())
    reg.register(ArtifactVerifyNode())
    reg.register(TextDiffNode())
    reg.register(UnitConvertNode())
    # data (csv.profile verified independently by stdlib csv)
    reg.register(CsvProfileNode(), verifier="csv.profile.alt")
    reg.register(CsvProfileVerifier())
    reg.register(DataQuerySqlNode())
    reg.register(DataDiffNode())
    # web
    reg.register(UrlInspectNode())
    reg.register(UrlToMarkdownNode())
    reg.register(PageLinksNode())
    reg.register(PageExtractNode())
    # documents
    reg.register(DocumentToMarkdownNode())
    reg.register(DocumentChunkNode())
    # api + json
    reg.register(OpenApiInspectNode())
    reg.register(DataTransformJsonNode())
    reg.register(OpenApiLintNode())
    reg.register(OpenApiDiffNode())
    reg.register(SchemaGenerateNode())
    # images
    reg.register(ImageInspectNode())
    reg.register(ImageTransformNode())
    # pdf + doc compare
    reg.register(PdfManipulateNode())
    reg.register(DocumentCompareNode())
    # data (2)
    reg.register(DataConvertNode())
    reg.register(DataValidateNode())
    reg.register(DataStatsNode())
    reg.register(DataDedupeNode())
    reg.register(ChartSpecNode())
    # code pack
    reg.register(RepoMapNode())
    reg.register(RepoLintNode())
    reg.register(RepoScanSecretsNode())
    # composition (verifiable artifact graph)
    reg.register(WorkflowComposeNode())
    # simulation & foresight (premium/A2A, guarded)
    reg.register(SimRunNode())
    # batch 2 — documents/identity/data/web/api
    reg.register(DocumentRedactPiiNode())
    reg.register(EmailValidateNode())
    reg.register(DataJoinNode())
    reg.register(DataCleanNode())
    reg.register(ObjectDiffNode())
    reg.register(SiteMapNode())
    reg.register(ApiToMcpNode())
    reg.register(McpValidateNode())
    # utility batch 3
    reg.register(TextStatsNode())
    reg.register(CsvToTableNode())
    reg.register(ReceiptVerifyNode())
    reg.register(DataPivotNode())
    reg.register(RobotsCheckNode())
    # llm (optional)
    if _HAS_LLM:
        reg.register(DocumentExtractJsonNode())
        reg.register(TextSummarizeNode())
    return reg
