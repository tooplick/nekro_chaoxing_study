from typing import Optional
from nekro_agent.services.agent.openai import gen_openai_chat_response

class AITiku:
    """基于 Nekro 模型组的 AI 答题系统"""

    def __init__(self, group_info: dict, timeout: float = 15.0):
        self.group_info = group_info
        self.timeout = timeout

    async def query(self, question: str, options: list[str], q_type: str) -> Optional[dict]:
        """调用模型组 API 返回答案
        返回格式: {"success": True, "answer": "回答内容"} 或者 None
        """
        if not self.group_info:
            return None

        # 构造类似原项目的高精确度 Prompt 结构
        import re
        import json
        
        def remove_md_json_wrapper(md_str):
            pattern = r'^\s*```(?:json)?\s*(.*?)\s*```\s*$'
            match = re.search(pattern, md_str, re.DOTALL)
            return match.group(1).strip() if match else md_str.strip()

        # 去除选项字母，防止大模型直接输出字母而非内容 (参照原项目)
        if options:
            cleaned_options = [re.sub(r"^[A-Z]\.?、?\s*", "", option) for option in options]
            options_text = "\n".join(cleaned_options)
        else:
            options_text = ""

        sys_prompt = ""
        if q_type == "single":
            sys_prompt = '本题为单选题，你只能选择一个选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{"Answer": ["答案"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料'
        elif q_type == 'multiple':
            sys_prompt = '本题为多选题，你必须选择两个或以上选项，请根据题目和选项回答问题，以json格式输出正确的选项内容，示例回答：{"Answer": ["答案1",\n"答案2",\n"答案3"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料'
        elif q_type == 'completion':
            sys_prompt = '本题为填空题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{"Answer": ["答案"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料'
        elif q_type == 'judgement':
            sys_prompt = '本题为判断题，你只能回答正确或者错误，请根据题目回答问题，以json格式输出正确的答案，示例回答：{"Answer": ["正确"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料'
        else:
            sys_prompt = '本题为简答题，你必须根据语境和相关知识填入合适的内容，请根据题目回答问题，以json格式输出正确的答案，示例回答：{"Answer": ["这是我的答案"]}。除此之外不要输出任何多余的内容，也不要使用MD语法。如果你使用了互联网搜索，也请不要返回搜索的结果和参考资料'

        user_prompt = f"题目：{question}"
        if options_text:
            user_prompt += f"\n选项：{options_text}"

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ]

        try:
            import asyncio
            from nekro_agent.api.core import logger
            logger.info(f"[AITiku] 正在请求大模型答题，题型={q_type}，题目={question[:20]}...")
            
            # 增加超时限制，防止模型无响应导致测验模块卡死 60s 以上
            result = await asyncio.wait_for(
                gen_openai_chat_response(
                    model=self.group_info.get("CHAT_MODEL"),
                    messages=messages,
                    api_key=self.group_info.get("API_KEY"),
                    base_url=self.group_info.get("BASE_URL")
                ),
                timeout=self.timeout
            )
            
            if result and result.response_content:
                raw_out = result.response_content.strip()
                try:
                    parsed = json.loads(remove_md_json_wrapper(raw_out))
                    answer_list = parsed.get("Answer", [])
                    # 组合成单一字符串返回
                    answer = "\n".join(answer_list).strip()
                except Exception:
                    # 如果 json 解析失败，降级返回原文
                    logger.warning("[AITiku] 无法将模型响应解析为指定 JSON 格式，回退到降级匹配")
                    answer = raw_out
                    
                logger.info(f"[AITiku] AI 返回答案: {answer}")
                return {"success": True, "answer": answer}
            else:
                logger.error(f"[AITiku] 大模型接口未返回任何有效内容，返回对象: {result}")
                
        except asyncio.TimeoutError:
            from nekro_agent.api.core import logger
            logger.warning(f"[AITiku] AI 题库答题超时 ({self.timeout}s)")
        except Exception as e:
            from nekro_agent.api.core import logger
            import traceback
            logger.error(f"[AITiku] AI 题库答题请求失败，出现异常: {str(e)}")
            logger.error(f"[AITiku] 请求失败详情:\n{traceback.format_exc()}")
            
        # 防止抛出异常或返回 None 导致无日志追踪
        from nekro_agent.api.core import logger
        logger.error(f"[AITiku] 题库响应结果异常或失败，放弃该题 (题目: {question[:20]}...)")
        return None
