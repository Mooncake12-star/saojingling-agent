"""记忆链路验证：不调真实大模型，用一个假模型验证
① 窗口只保留最近 4 轮  ② 超出的旧消息折进摘要  ③ 摘要落库、重启后能读回来
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.react_agent import ReactAgent
from db.session import session_scope
from models.user import LEGACY_USER_ID
from repositories.conversation import ConversationRepository


class FakeResp:
    def __init__(self, content):
        self.content = content


class FakeModel:
    """假的 chat model：把输入长度当成摘要，方便断言"""
    def __init__(self):
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return FakeResp(f"[摘要v{self.calls}] 用户问过扫地机器人故障，型号 X3 Ultra。")


SID = "probe-memory-session"


def reset(sid):
    # 改走 repository：db/database.py 已删除，裸连接不再存在
    # P2 起会话属于某个用户，探测器沿用 legacy 账号（id=1）
    with session_scope() as session:
        ConversationRepository(session).clear(LEGACY_USER_ID, sid)


async def main():
    reset(SID)
    agent = ReactAgent(session_id=SID)
    agent.model = FakeModel()
    print(f"初始: rows={len(agent._rows)} window={len(agent.conversation_history)} summary={agent.summary!r}")

    for turn in range(1, 8):
        agent._save_message("human", f"第{turn}轮问题")
        agent._save_message("ai", f"第{turn}轮回答")
        agent._rebuild_window()
        before = len(agent._rows)
        await agent._fold_old_messages()
        print(
            f"第{turn}轮后: rows={len(agent._rows)} (折叠前 {before}) "
            f"window={len(agent.conversation_history)} fold_calls={agent.model.calls} "
            f"summary={agent.summary[:22]!r}"
        )

    # 重新加载，模拟服务重启
    agent2 = ReactAgent(session_id=SID)
    agent2.model = FakeModel()
    print(f"\n重启后: window={len(agent2.conversation_history)} "
          f"summary={agent2.summary[:22]!r}")
    print("system_prompt 含摘要:", "历史对话摘要" in agent2._build_system_prompt())
    print("窗口内容:", [m.content for m in agent2.conversation_history][:4], "...")

    reset(SID)


asyncio.run(main())
