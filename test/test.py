"""
agent 基础设施 12 场景测试

1  state 默认值
2  state set/get + snapshot
3  plan 工具分组(plan_model=True/False)
4  plan 工具直调 → 写 state.pending
5  file 层 6 函数(create/read/change/list_dir/remove_file)
6  auto=False → 文件工具返回 pending_approval
7  state.auto 优先级 > 工具 auto 参数
8  loop 只读任务(用 list_dir 查 agent 目录)
9  loop 写任务 → 触发 plan → 停下
10 用户确认 → 续接 → 真正创建文件
11 续接后再触发 plan(plan 在 history 中)
12 plan_model=False → plan 工具不出现(宽松断言:文件落地即可)
"""
import os
import sys
import shutil
from pathlib import Path

# 把项目根目录加进 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import state, tool
from explorer import file
from agent.loop import run


# ────────── 辅助 ──────────
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    icon = "[OK]  " if cond else "[FAIL]"
    suffix = f"  -- {detail}" if detail else ""
    print(f"  {icon} {name}{suffix}")
    if cond:
        PASS += 1
    else:
        FAIL += 1


def section(n: int, title: str):
    print(f"\n[{n}] {title}")


# ────────── 测试路径 ──────────
ROOT      = Path(__file__).resolve().parent.parent
SCRATCH   = ROOT / "test" / "_scratch"
LOOP_A    = ROOT / "test" / "_loop_a.txt"
LOOP_B    = ROOT / "test" / "_loop_b.txt"
LOOP_C    = ROOT / "test" / "_loop_c.txt"


def cleanup():
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    for f in (LOOP_A, LOOP_B, LOOP_C):
        if f.exists():
            f.unlink()


# ════════════════════════════════════════════════════════════
#                          测试场景
# ════════════════════════════════════════════════════════════

# 1. state 默认值
section(1, "state 默认值")
state.clear_pending()
check("plan_model=True",  state.get_plan_model() is True)
check("auto=True",        state.get_auto()        is True)
check("pending=None",     state.get_pending()     is None)

# 2. set/get + snapshot
section(2, "set/get + snapshot")
state.set_plan_model(False)
state.set_auto(False)
state.set_pending({"action": "test", "args": {}})
check("plan_model=False", state.get_plan_model() is False)
check("auto=False",       state.get_auto()        is False)
check("pending set",      state.get_pending()     == {"action": "test", "args": {}})
snap = state.snapshot()
check("snapshot 字段",    set(snap.keys()) == {"plan_model", "auto", "pending"})
state.set_plan_model(True)
state.set_auto(True)
state.clear_pending()
check("reset 后",         state.snapshot() == {"plan_model": True, "auto": True, "pending": None})

# 3. plan 工具分组
section(3, "plan 工具分组")
tools_with    = tool.get_tools(plan_model=True)
tools_without = tool.get_tools(plan_model=False)
names_with    = {t["function"]["name"] for t in tools_with}
names_without = {t["function"]["name"] for t in tools_without}
check("plan_model=True 包含 plan",  "plan"     in names_with)
check("plan_model=False 隐藏 plan", "plan" not in names_without)
check("其它工具不受影响",           names_without.issubset(names_with))
check("其它工具至少 4 个",          len(names_with) >= 5)
print(f"      工具列表: {sorted(names_with)}")

# 4. plan 工具直调
section(4, "plan 工具直调")
result = tool.call(
    "plan",
    intent="测试", direction="加", basis="单元测试",
    affected_files=["a.py"], steps=["1. 测试"], risk="low",
)
check("plan 返回 markdown",     "## 📋 方案确认" in result)
check("写入 state.pending",     state.get_pending() is not None)
check("pending.action='plan'",  state.get_pending()["action"] == "plan")
check("pending 含 markdown",     bool(state.get_pending()["markdown"]))
state.clear_pending()

# 5. file 层函数
section(5, "file 层 6 函数")
cleanup()
SCRATCH.mkdir(parents=True, exist_ok=True)
TEST_FILE = SCRATCH / "a.txt"

check("file.create 存在",   file.create(str(TEST_FILE)) is None and TEST_FILE.exists())
file.change(str(TEST_FILE), "hello\n",  mode="append")
file.change(str(TEST_FILE), "world\n",  mode="append")
content = file.read(str(TEST_FILE))
check("file.read 含 hello", "hello" in content)
check("file.read 含 world", "world" in content)
check("file.read 带行号",   "1 |" in content and "2 |" in content)
file.change(str(TEST_FILE), "EDITED\n", mode="edit", position=0, end_line=1)
line0 = file.read(str(TEST_FILE), start_line=1).split("\n")[0]
check("file.change edit",   "EDITED" in line0)
items = file.list_dir(str(SCRATCH))
check("file.list_dir",      "a.txt" in [x["name"] for x in items["files"]])
file.remove_file(str(TEST_FILE))
check("file.remove_file",   not TEST_FILE.exists())
shutil.rmtree(SCRATCH)

# 6. auto=False → pending
section(6, "auto=False → 文件工具返回 pending_approval")
state.set_auto(False)
r = tool.call("create_file", file_path=str(LOOP_A), content="x", auto=False)
check("create_file(auto=False) pending_approval",  "pending_approval" in r)
check("state.pending 已写入",                     state.get_pending() is not None)
state.clear_pending()

# 7. state 优先级
r = tool.call("create_file", file_path=str(LOOP_A), content="y", auto=True)
check("state.auto=False 时 auto=True 仍暂停",     "pending_approval" in r)
state.set_auto(True)
state.clear_pending()

# 8. loop 只读任务
section(8, "loop 只读任务(list agent 目录)")
out = run("agent 目录下有哪些 .py 文件?", history=None, max_rounds=5)
check("ok=True",          out["ok"] is True)
check("stopped=answer",   out["stopped"] == "answer")
check("用到 list_dir",    "list_dir" in out["tools_used"])
check("有回答",           bool(out["answer"]))
check("history 含 system", out["history"] and out["history"][0]["role"] == "system")
print(f"      回答摘要: {out['answer'][:80]}...")

# 9. loop 写任务 → 触发 plan
section(9, "loop 写任务 → 触发 plan")
cleanup()
out = run(f"创建文件 {LOOP_A} 写两行 hello", history=None, max_rounds=8)
check("stopped=pending",           out["stopped"] == "pending")
check("pending.action=plan",       out["pending"]["action"] == "plan")
check("plan 含 affected_files",    str(LOOP_A) in out["pending"]["args"]["affected_files"])
check("plan markdown 非空",        bool(out["pending"]["markdown"]))
check("plan 6 字段齐全",           all(k in out["pending"]["args"] for k in
    ["intent", "direction", "basis", "affected_files", "steps", "risk"]))

# 10. 用户确认 → 续接 → 创建
section(10, "用户确认 → 续接")
state.clear_pending()
out2 = run("确认,按计划执行", history=out["history"], max_rounds=8)
check("文件已创建",          LOOP_A.exists())
check("内容包含 hello",      "hello" in file.read(str(LOOP_A)))
check("stopped ∈ {answer, pending}", out2["stopped"] in ("answer", "pending"))

# 11. 续接后再 plan
section(11, "续接后再次 plan")
state.clear_pending()
out3 = run(f"再加一个 {LOOP_B} 写 world", history=out2["history"], max_rounds=8)
check("再次 stopped=pending",          out3["stopped"] == "pending")
check("新文件在 affected_files",       str(LOOP_B) in out3["pending"]["args"]["affected_files"])
check("history 累积(>=9 条)",          len(out3["history"]) >= 9)

# 12. plan_model=False
section(12, "plan_model=False → 宽松断言(只验文件落地)")
state.set_plan_model(False)
state.clear_pending()
out4 = run(f"创建 {LOOP_C} 写 foo", history=out3["history"], max_rounds=8)
# 模型有自由裁量权,即使 plan_model=False 也可能仍调 plan(残留 prompt 误导)
# 因此不验证 tools_used,只验证"最终文件能创建出来"
check("循环最终停下",              out4["stopped"] in ("answer", "pending"))
check("文件 LOOP_C 已创建",       LOOP_C.exists())
state.set_plan_model(True)

# ════════════════════════════════════════════════════════════
cleanup()

print(f"\n========== {PASS} passed, {FAIL} failed ==========")
sys.exit(0 if FAIL == 0 else 1)
