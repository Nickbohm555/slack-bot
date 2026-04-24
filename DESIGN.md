# Design

I spent quite a bit of time iterating on various ideas with this Slack agent, and I wanted to take the time to walk through it here. No AI was used in writing this design, all handwritten :)

## Slack Layer

To start, I needed to find a way to **connect Slack** to my agent. I had not done this before, so I did some research, and one of the main ways I found I could do this was by using a **WebSocket connection with Slack Bolt**:

https://github.com/slackapi/bolt-python

I figured this reduces the need to expose and secure a public webhook endpoint or use a built-in feature LangSmith had created. I settled on the WebSocket approach and had an event listener in my Python code under `/src/api_service/slack/app.py`, which listens to incoming messages and sends data such as the message, the `user_id`, and the Slack channel name.

## Conversation History

Now that I had a Slack connection, I wanted to first tackle memory and conversation history before starting. Since I have built deep agent applications at work, I knew I could use a checkpointer in Postgres to store memory scoped to a specific `thread_id`:

https://reference.langchain.com/python/langgraph.store.postgres

Now the problem was, how do we ensure we are using the same thread for a given user, and how do we make sure the user can start new chats? Initially, I thought of keeping a hardcoded `thread_id` and rotating it when there is some condition for starting a new chat, but then all users would have the same `thread_id`, so this does not work. It had to be something unique to the user.

### Thread Management

I decided to create a Postgres table for storing a given `thread_id` for a user. Here was the data flow. When a user sends a message, it goes to the event listener. Assuming the user does not initiate a new chat, it will use the existing `thread_id`. So we pass in a **combination of `user_id` and `channel_id` from the event listener**. We can use this to create a unique `thread_id` by having the combined `user_id` and `channel_id` value as our primary key and the generated `thread_id` as our value to retrieve. If there is no existing row for this `user_id`/`channel_id` value, then we create the row.

### Starting a New Chat

If we want a user to start a new chat, they can just send a message `'new'`. After the event listener, one of our conditions is whether we send `'new'`. If we do, we get the existing `thread_id` row in the DB and change the value. This way, our chat history can be cleared.

### Handling Long Conversations

A few other points on conversation history. I am using a deep agent that already has a built-in compaction tool to deal with long messages:

https://reference.langchain.com/python/deepagents/middleware/summarization

I had another solution as well. For every chat sent, before sending the message back over the Slack channel, the code checks what our conversation length is, estimates the token usage of the conversation message, and sees if it passes the threshold I hardcoded. If it does, we append an extra sentence to our Slack UI giving a warning.

## User Experience

There were a few different things I was thinking of for streaming. In openClaw, when a user is waiting on a message, they have the `...` to simulate a person texting. I thought to have a placeholder similar in my Slack bot where, when a message is sent to our app through the event listener, I have a placeholder through the Slack Bolt framework that starts my message as `'thinking'` until something is returned. A further enhancement could be streaming the todo list planner tasks as they are completed and using that as the placeholder. I did not get around to doing this.

## Agent Design

Ok, now for the interesting part.

### Tool Design

I had to think through how to build my agent. I knew there were a few foundational things I needed to do first. I had to expose whatever agent I decided on to tools. I used 3 basic tools for gaining insight into a structured DB using:

https://docs.langchain.com/oss/python/langchain/tools

- `@tool("sql_db_list_tables")`
  `sql_db_list_tables`: Input is an empty string, output is a comma-separated list of tables in the database.
- `@tool("sql_db_schema")`
  `sql_db_schema`: Input to this tool is a comma-separated list of tables, output is the schema and sample rows for those tables. Be sure that the tables actually exist by calling `sql_db_list_tables` first. Example input: `table1, table2, table3`
- `@tool("sql_db_query")`
  `sql_db_query`: Input to this tool is a detailed and correct SQL query using only tables and columns already confirmed by `sql_db_list_tables` or `sql_db_schema` in the current conversation. The backend returns at most 5 rows and truncates large text cells. If the query is not correct, an error message will be returned detailing to run the other 2 tools to make sure the query is correct.

Now, the reason I chose these 3 specifically is because of some specific tool design principles. I had to make sure my tools were token efficient, easy to call, namespaced to our group type (SQL calls), and had a clear use case:

https://www.anthropic.com/engineering/writing-tools-for-agents

To do this, I used `sql` at the start of each tool name. I have a clear use case for each: before starting, use `list_tables` to see what could be relevant, use `db_schema` to dive deeper into a specific table, and use `db_query` to ask something specific and extract data. These simple tools also match what is recommended in LangChain docs for building SQL agents:

https://docs.langchain.com/oss/python/langchain/sql-agent

This also matches my experience connecting SQL functionality to deep agents at work.

Here are a few further enhancements I made. `db_query` was at first stuck in a loop every now and then where it would try multiple incorrect SQL queries before eventually getting it right. A simple but effective solution for this was adding tool failure logic where, if the SQL query did not succeed, then the tool would send a custom message back saying to search the specific table using the other tool again before trying. This way, we did not get stuck in any unnecessary loops. Another issue was dealing with huge results that cluttered the context. I had a truncated output for my tool call to only send back 500 characters, as well as limit my queries to 8 rows max for this use case.

It was extremely tempting to keep adding more specific SQL tools, but every time I tried, performance degraded, which I will discuss later. Another method I spent time trying was skills. I use it at work, and I also saw it in recent LangChain documents about how to progressively disclose context about a schema or problem through adding skills to my deep agent:

https://docs.langchain.com/oss/python/langchain/multi-agent/skills-sql-assistant

However, this was not very fruitful for me. Part of the reason was I tried to optimize for latency as well. Since this is a Slack chatbot, my thought process is that users will probably not want to wait for 1 minute+ for an answer. I think later models like GPT-5 are quite good at knowing when to invoke skills, but for a model like 4.1 mini, where it loses track of things easily, adding skills was extremely brittle, which aligned with my eval scores as well. I tried adding middleware skills, then later just adding them to the built-in deep agent skills, but it always added more complexity with worse performance.

### Agent-Specific Design

Ok, so other than these 3 tools I needed to know what agent to use: a `create_agent` single agent, a deep agent allowing multiple subagents, a workflow. There were many options. At first I figured a simple single agent with 3 tools should suffice. However, the questions required somewhat detailed planning, and I also wanted to solve the compaction issue. Both of these issues are solved with deep agents due to the built-in planning tool and compaction middleware. I used the 4.1 mini model with the todo tool to attempt to get the best of both worlds with lightning-quick delivery while still staying on course for complex questions. Another benefit of choosing deep agents for me was if I had a large SQL output I could use the built-in file storage system to dump the result instead of clogging memory.

Now that I decided on a deep agent, there were still a few things to consider. 1. I did not want to include subagents. I felt this would add unnecessary complexity. 2. I needed the agent flow to be sequential. The reason for this was that a lot of our queries are built off previous information. This reminded me of a previous article I read a while back where Devin AI argued that running subagents in parallel can, given the circumstances, degrade performance if information is reliant on each other:

https://cognition.ai/blog/dont-build-multi-agents

Models have improved a lot since then, but since in my example I am using an older model, I thought the same principles could apply. I did not want to rely on parallel tool calls or agent calls. To solve both of these issues I added middleware to attach to all agent calls to ensure there are no parallel model calls, as well as to not allow subagent handoffs. I think the 4.1 mini model does not even have this capability, but it could still be worth it for larger models.

## Evaluations

This is, I think, the most important aspect of the building process. Without knowing what is right and wrong, how can we hope to build effective agents? Everything I have talked about here would be speculation without clear evals. I used our 7 question/answer pairs given, created a separate Docker container to run an eval pipeline which takes a JSON of these questions, and for each pair it uses the question as the message input for our agent, uses a random checkpointer to have a clear context, and then makes an Excel doc with columns: 1. `question` 2. `answer` 3. `my_answer` 4. `correctness` 5. `tool_calls` 6. `latency`. Each answer is graded using LLM-as-a-judge to get a general correctness score for how accurate my answer was to the truth. This was scored 0-1. However, I figured I could also trace the agent's train of thought by getting the order in which tool calls were made for the agent. This way I could diagnose what went wrong. This is aligned with a few articles I read, such as:

https://docs.langchain.com/langsmith/evaluate-complex-agent

Ok, so now I could see the average correctness score, which gave me an indication of whether or not I was heading in the right direction. However, I noticed my agent was still extremely brittle. I was trying to account for every use case, which was confusing the agent and degrading the score. I had to start from scratch again with no skills and a reduced system prompt. I wanted to strategically use my todo tool and my 3 existing tools to account for different use cases. I had a few ideas from here.

### Autoresearch

Since I now had the context for agent outputs, tool call order, and correctness, this seemed ripe for using autoresearch:

https://github.com/karpathy/autoresearch

The reason is because there is a clear metric of whether or not we are improving: correctness. I applied these concepts of 1. running evals 2. analyzing what went wrong 3. making code changes to build out my own autoresearch where I had my agent only update the prompt and not touch skills, tools, etc. This way there could be more clarity on what was working. Since I had already built the evals for this, I just needed a bash loop where we run the evals first, then have my coding agent read the eval summary and have an analyzer prompt look at the tool call order, what was wrong in my output, and the prompt to determine what went wrong.

This worked pretty well and took me from ~20 percent correctness to ~60-80% correctness. I could also see it was taking about 20-30 seconds to complete as well as 10-15 tool calls, which all seemed reasonable to me. In the beginning it was using 3-4 tool calls, which was not nearly enough to understand the question/answer well enough, and at one point it was overcalling tools with retry logic and taking 20-30 tools. I found a happy middle ground through some strategic implementations already discussed, like SQL tool retry logic as well as prompt engineering. I was desperately trying to get to 90+, but the more I tried to account for certain examples, the worse other query answers became. I even tried adding more specific tools with hardcoded aspects of executing SQL queries, but this made my correctness degrade further. I then tried changing my model to GPT-5 and 5.1 mini, but the latency was 2-3x.

## Change of Thought

This last improvement I was trying to achieve reminded me of this article:

https://vercel.com/blog/we-removed-80-percent-of-our-agents-tools

They talked about how they were banging their heads trying to account for every edge case in their traditional RAG system, while the solution in front of them the whole time was a filesystem RAG storage with extremely detailed SQL instructions. I do not think having a better prompt would have drastically improved my performance from where I am at. In order to get to the 90+ percent correctness I think I would need to do something similar to what Vercel did here and what we ended up doing with SharePoint retrieval, which is having a detailed file storage system where our agent can search for the specific instructions to this type of SQL problem using `ls`, `grep`, `read`, etc. Treat the agent like an analyst who has access to how this DB works. If I had more time, I am curious if this could solve the issue. I am not sure how bad latency would be, though. A lot to consider.

## Security

Besides the webhook auth issue discussed earlier, I wanted to address a few other ideas I had started thinking about as well. What if we do not want certain users to have access to tables? This can be easily scoped through this sort of workflow. From the Slack payload we have the `user_id`, and we can get a list of table/server access from here. When we build up the `sql_execute` tool, we can create a factory function to have as an input the list of tables we can access. If the user tries writing a query for this table, we automatically return a tool message back saying table access denied for `X`.

Overall, this was a fun experience, and I got to learn a lot more in the process of iterating.
