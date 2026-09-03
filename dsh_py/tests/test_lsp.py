"""lsp seam 契约单测（对照 dsh 临时冒烟脚本，正式入库）。

覆盖：品牌工厂、扩展名提取、provider 注册/选择、结构化错误、stdlib 归一化
（定位/range/hover/marked）、位置编码协商、方法映射。均不依赖真实 LSP 后端。
"""

from __future__ import annotations

from dsh_py.services.lsp import (
    LSP_UNAVAILABLE,
    LspError,
    LspPosition,
    LspProvider,
    LspProviderId,
    LspQueryRequest,
    LspQueryResult,
    LspRange,
    LspService,
    _ext_of,
)
from dsh_py.services.lsp_stdio import (
    _is_pos,
    _is_range,
    _negotiate_position_encoding,
    _render_marked,
    _request_method,
    _to_range,
    normalize_hover,
    normalize_locations,
)


class _FakeProvider(LspProvider):
    """测试用 provider：记录收到的查询，同步返回结果（对齐 LspService.query 不 await 的约定）。"""

    def __init__(self, id: str, ext_map: dict[str, str], record: list | None = None) -> None:
        self.id = id
        self.extensionToLanguage = ext_map
        self._record = record

    def query(self, request, signal=None):
        if self._record is not None:
            self._record.append(request)
        r = LspQueryResult()
        r.kind = "locations"
        r.locations = []
        r.resolvedWorkspaceUri = request.workspaceRoot
        return r


async def test_provider_id_and_ext() -> None:
    assert LspProviderId("py") == "py"
    assert _ext_of("a/b/c.py") == ".py"
    assert _ext_of("NoExt") == ""
    assert _ext_of("a/PY.py") == ".py"
    print("  ✓ LspProviderId / _ext_of 正确")


async def test_error_default_code() -> None:
    e = LspError("boom")
    assert e.code == LSP_UNAVAILABLE
    assert e.message == "boom"
    print("  ✓ LspError 默认 code=LSP_UNAVAILABLE")


async def test_service_register_and_query() -> None:
    svc = LspService()
    # 空 id 拒绝
    try:
        svc.registerProvider(_FakeProvider("", {".py": "python"}))
        raise AssertionError("应拒绝空 id 注册")
    except LspError as e:
        assert e.code == "LSP_REGISTRATION"
    # 正常注册返回 unregister 闭包；记录注入的 languageId
    rec: list = []
    unreg = svc.registerProvider(_FakeProvider("py-lsp", {".py": "python"}, rec))
    assert callable(unreg)
    # 扩展冲突拒绝
    try:
        svc.registerProvider(_FakeProvider("other", {".py": "python"}))
        raise AssertionError("应拒绝扩展冲突注册")
    except LspError as e:
        assert e.code == "LSP_REGISTRATION"
    # query 命中 python provider 并注入 languageId
    res = svc.query(LspQueryRequest(operation="hover", filePath="/x/main.py",
                                    position=LspPosition(0, 0), workspaceRoot="/x"))
    assert res.resolvedWorkspaceUri == "/x"
    assert len(rec) == 1 and rec[0].languageId == "python"
    # 无 provider 扩展 → 不可用
    try:
        svc.query(LspQueryRequest(operation="hover", filePath="/x/a.rs",
                                  position=LspPosition(0, 0), workspaceRoot="/x"))
        raise AssertionError("应抛 LSP_UNAVAILABLE")
    except LspError as e:
        assert e.code == LSP_UNAVAILABLE
    # 注销后查询失败
    unreg()
    try:
        svc.query(LspQueryRequest(operation="hover", filePath="/x/main.py",
                                  position=LspPosition(0, 0), workspaceRoot="/x"))
        raise AssertionError("注销后查询应抛 LSP_UNAVAILABLE")
    except LspError as e:
        assert e.code == LSP_UNAVAILABLE
    print("  ✓ LspService 注册/选择/注销正确")


async def test_request_method_and_encoding() -> None:
    assert _request_method("goToDefinition") == "textDocument/definition"
    assert _request_method("findReferences") == "textDocument/references"
    assert _request_method("goToImplementation") == "textDocument/implementation"
    assert _request_method("hover") == "textDocument/hover"
    try:
        _request_method("bogus")
        raise AssertionError("未知操作应 KeyError")
    except KeyError:
        pass
    assert _negotiate_position_encoding(None) == "utf-16"
    assert _negotiate_position_encoding("utf-16") == "utf-16"
    try:
        _negotiate_position_encoding("utf-32")
        raise AssertionError("不支持编码应抛 LspError")
    except LspError as e:
        assert e.code == "LSP_PROTOCOL"
    print("  ✓ 方法映射 / 位置编码协商正确")


async def test_range_and_location_normalize() -> None:
    r = _to_range({"start": {"line": 1, "character": 2}, "end": {"line": 3, "character": 4}})
    assert isinstance(r, LspRange)
    assert r.start == LspPosition(1, 2) and r.end == LspPosition(3, 4)
    assert _is_pos({"line": 0, "character": 0})
    assert not _is_pos({"line": 0})
    assert not _is_pos("x")
    assert _is_range({"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 1}})
    assert not _is_range({"start": {"line": 0, "character": 0}})

    assert normalize_locations(None) == []
    loc = [{"uri": "file:///a", "range": {"start": {"line": 0, "character": 0},
                                          "end": {"line": 1, "character": 1}}}]
    out = normalize_locations(loc)
    assert len(out) == 1 and out[0].uri == "file:///a"
    link = [{"targetUri": "file:///b", "targetSelectionRange": {"start": {"line": 0, "character": 0},
                                                                "end": {"line": 1, "character": 1}}}]
    out2 = normalize_locations(link)
    assert out2[0].uri == "file:///b" and out2[0].range.start == LspPosition(0, 0)
    # 单 dict（非列表）也解析
    assert len(normalize_locations(loc[0])) == 1
    # 非对象元素 / 既非 Location 也非 LocationLink → 畸形
    for bad in [[1], [{"foo": "bar"}]]:
        try:
            normalize_locations(bad)
            raise AssertionError("应抛 LSP_MALFORMED_RESPONSE")
        except LspError as e:
            assert e.code == "LSP_MALFORMED_RESPONSE"
    print("  ✓ range / location 归一化正确")


async def test_hover_normalize() -> None:
    assert normalize_hover(None) is None
    try:
        normalize_hover("not-dict")
        raise AssertionError("非 dict 应抛错")
    except LspError as e:
        assert e.code == "LSP_MALFORMED_RESPONSE"
    try:
        normalize_hover({"contents": None})
        raise AssertionError("无 contents 应抛错")
    except LspError as e:
        assert e.code == "LSP_MALFORMED_RESPONSE"
    # 字符串内容
    h = normalize_hover({"contents": "hello"})
    assert h == {"contents": "hello", "range": None}
    # 空字符串 → None
    assert normalize_hover({"contents": ""}) is None
    # 列表内容
    h2 = normalize_hover({"contents": [{"language": "py", "value": "x"}, "plain"]})
    assert h2["contents"] == "```py\nx\n```\n\nplain"
    # 带 range
    h3 = normalize_hover({"contents": "t", "range": {"start": {"line": 0, "character": 0},
                                                     "end": {"line": 1, "character": 1}}})
    assert h3["range"] == LspRange(LspPosition(0, 0), LspPosition(1, 1))
    print("  ✓ hover 归一化正确")


async def test_render_marked() -> None:
    assert _render_marked("plain") == "plain"
    assert _render_marked({"language": "py", "value": "x"}) == "```py\nx\n```"
    print("  ✓ _render_marked 正确")


async def main() -> None:
    print("== test_lsp ==")
    await test_provider_id_and_ext()
    await test_error_default_code()
    await test_service_register_and_query()
    await test_request_method_and_encoding()
    await test_range_and_location_normalize()
    await test_hover_normalize()
    await test_render_marked()
    print("OK: lsp seam 契约单测通过")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
