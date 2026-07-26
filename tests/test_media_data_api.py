"""Tests for image, pdf, data(2) and api(2) endpoints with real generated binaries."""
import base64
import io

from contract import ArtifactRequest, VerificationLevel


def _req(endpoint, **inp):
    return ArtifactRequest(endpoint=endpoint, input=inp)


def _png_b64(w=8, h=6, color=(255, 0, 0)):
    from PIL import Image
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO(); im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _pdf_b64(pages=2):
    import pypdf
    w = pypdf.PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO(); w.write(buf)
    return base64.b64encode(buf.getvalue()).decode()


def test_image_inspect(runtime):
    env = runtime.execute(_req("image.inspect", content_b64=_png_b64(10, 5)))
    assert env.ok
    assert env.result["width"] == 10 and env.result["height"] == 5
    assert env.result["format"] == "PNG"


def test_image_transform_resize(runtime):
    env = runtime.execute(_req("image.transform", content_b64=_png_b64(20, 20),
                               op="resize", width=8, height=8, format="PNG"))
    assert env.ok
    assert env.result["out_width"] == 8 and env.result["out_height"] == 8
    assert env.result["out_bytes"] > 0
    assert len(env.artifacts) == 1
    # deterministic PNG encode → reproduced
    assert env.validation.level == VerificationLevel.L3_REPRODUCED


def test_pdf_manipulate_info(runtime):
    env = runtime.execute(_req("pdf.manipulate", content_b64=_pdf_b64(3), op="info"))
    assert env.ok
    assert env.result["pages"] == 3


def test_pdf_manipulate_extract(runtime):
    env = runtime.execute(_req("pdf.manipulate", content_b64=_pdf_b64(4),
                               op="extract_pages", pages=[1, 2]))
    assert env.ok
    assert env.result["out_pages"] == 2
    assert len(env.artifacts) == 1


def test_document_compare(runtime):
    env = runtime.execute(_req("document.compare", a="alpha\nbeta\ngamma", b="alpha\nBETA\ngamma"))
    assert env.ok
    assert env.result["identical"] is False
    assert env.result["changed_lines"] >= 1


def test_data_convert_csv_to_json(runtime):
    env = runtime.execute(_req("data.convert", csv="a,b\n1,2\n3,4\n", to="json"))
    assert env.ok
    assert env.result["row_count"] == 2
    import json
    assert json.loads(env.result["output"])[0]["a"] == "1"


def test_data_validate(runtime):
    schema = {"type": "object", "properties": {"age": {"type": "integer", "minimum": 0}}, "required": ["age"]}
    env = runtime.execute(_req("data.validate", rows=[{"age": 5}, {"age": -1}, {"name": "x"}], schema=schema))
    assert env.ok
    assert env.result["total"] == 3
    assert env.result["valid"] == 1
    assert env.result["invalid"] == 2


def test_data_stats(runtime):
    env = runtime.execute(_req("data.stats", rows=[{"x": 2}, {"x": 4}, {"x": 6}]))
    assert env.ok
    assert env.result["stats"]["x"]["mean"] == 4.0
    assert env.result["stats"]["x"]["min"] == 2.0


def test_data_dedupe(runtime):
    env = runtime.execute(_req("data.dedupe", rows=[{"a": 1}, {"a": 1}, {"a": 2}]))
    assert env.ok
    assert env.result["unique_rows"] == 2
    assert env.result["duplicate_rows"] == 1


def test_chart_spec_valid(runtime):
    env = runtime.execute(_req("chart.spec", rows=[{"k": "a", "v": 1}], x="k", y="v", mark="bar"))
    assert env.ok
    assert env.result["spec"]["$schema"].startswith("https://vega.github.io/schema/vega-lite")
    assert env.result["spec"]["encoding"]["x"]["field"] == "k"


def test_openapi_lint_finds_issues(runtime):
    spec = {"openapi": "3.0.0", "info": {"title": "x"}, "paths": {"/a": {"get": {}}}}
    env = runtime.execute(_req("openapi.lint", spec=spec))
    assert env.ok
    # missing responses → error; missing operationId/summary → warnings
    assert env.result["error_count"] >= 1
    assert env.result["warning_count"] >= 1


def test_openapi_diff_breaking(runtime):
    old = {"paths": {"/a": {"get": {}}, "/b": {"get": {}}}}
    new = {"paths": {"/a": {"get": {}}}}
    env = runtime.execute(_req("openapi.diff", old=old, new=new))
    assert env.ok
    assert env.result["breaking"] is True
    assert "GET /b" in env.result["removed"]


def test_schema_generate(runtime):
    env = runtime.execute(_req("schema.generate", example={"a": 1, "b": "x", "c": [1, 2]}))
    assert env.ok
    s = env.result["schema"]
    assert s["type"] == "object"
    assert s["properties"]["a"]["type"] == "integer"
    assert s["properties"]["c"]["type"] == "array"
