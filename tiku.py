from typing import Optional
from nekro_agent.services.agent.openai import gen_openai_chat_response

class AITiku:
    """基于 Nekro 模型组的 AI 答题系统"""

    def __init__(self, group_info: dict):
        self.group_info = group_info

    async def query(self, question: str, options: list[str], q_type: str) -> Optional[dict]:
        """调用模型组 API 返回答案
        返回格式: {"success": True, "answer": "回答内容"} 或者 None
        """
        if not self.group_info:
            return None

        prompt = f"""作为一个专业的在线答题助手。你需要根据题目和选项给出正确答案。

题型：{q_type}
题目：{question}
选项：
{chr(10).join([f"{i+1}. {opt}" for i, opt in enumerate(options)]) if options else "无固定选项"}

请直接输出正确答案，如果是单选题或多选题，只能输出正确选项的内容本身（不要带序号）。如果是判断题，请回答"正确"或"错误"。
如果是填空题，直接给出填空内容。
注意：不需要任何额外的解释和抱歉的话语，只输出最终答案字符串即可。"""

        try:
            from nekro_agent.api.core import logger
            logger.info(f"[AITiku] 正在请求大模型答题，题型={q_type}，题目={question[:20]}...")
            
            result = await gen_openai_chat_response(
                model=self.group_info.get("CHAT_MODEL"),
                messages=[{"role": "user", "content": prompt}],
                api_key=self.group_info.get("API_KEY"),
                base_url=self.group_info.get("BASE_URL")
            )
            
            if result and result.response_content:
                answer = result.response_content.strip()
                logger.info(f"[AITiku] AI 返回答案: {answer}")
                return {"success": True, "answer": answer}
            
        except Exception as e:
            from nekro_agent.api.core import logger
            logger.error(f"[AITiku] AI 题库答题异常: {e}")
            
        return None
