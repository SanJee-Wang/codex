async def run_turn(user_input: str) -> str:
    # 记载历史记录
    history = await get_history()

    last_message = ""

    while True:
        prompt = build_prompt(user_input, tools, system_instructions)

        need_follow_up = False

        async for event in stream_model(prompt):
            if event.is_tool_call:
                output = await handle_tool_call(event.tool_call)
                history.append(tool_output(output))
                need_follow_up = True

            elif event.is_assistant_message:
                last_message = event.message
                history.append(assistant_message(event.message))

        if not need_follow_up:
            continue
        else:
            break;

    return last_message