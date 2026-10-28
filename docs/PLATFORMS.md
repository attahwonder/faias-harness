# Platform documentation survey

Harness version 0.2.0. This file records what the public documentation of six agent platforms says about the seven capabilities of the target interface (`docs/ARCHITECTURE.md`, section 3).

## What this is

- Two independent model passes read vendor documentation. For each platform and capability, each pass named the mechanism that the documentation describes. It also copied a quote from a page.
- On 16 October 2026, a script fetched the raw page for each quote. It tested whether the quote occurs on the page. The test ignores case, spacing, quote marks and dash styles. It follows no link and reads only the vendor documentation hosts and, for the OpenTelemetry specification, the project's own repository.
- The tables below list only the rows where the quote was found on the page. The mechanism label is a short note from a model pass. Nobody has checked the label. Only the quote is verified. A quote that this file cuts short ends with " ..."; the part shown is still a quote from the page.
- Where both passes found a quote for the same capability, both quotes are listed. The column "Passes with a verified quote" shows how many of the two passes have a verified quote for that capability. A value of 1 means that the other pass gave no quote, or gave a quote that the check did not find.
- A second round on 16 October 2026 asked both passes about one gap only: `control` for the OpenAI Agents SDK and Responses API. In its rows, the mechanism label starts with the control: stop, rollback, version switch or failure injection.

## What this is not

- Version 0.5 adds channel adapters written for the channels of some of these platforms. They are `directline` for Copilot Studio and `openai-responses` for the OpenAI Responses API and Azure AI Foundry agents. A third, `a2a`, is for a platform that offers A2A. None is tested against the platform it is written for. Each is tested against a local stand-in only. The harness supports no platform (`faias_harness/adapters/README.md`). This survey is not a test of any adapter.
- A quote that is found shows that the page contains those words. It does not show that the capability works in the way the harness needs. The quote can describe a related feature. A plan, a region or a preview status can limit a feature.
- Documentation changes. A page can move or change after 16 October 2026. Read the page again before you rely on a row.
- A capability that is not verified here is not evidence that the platform lacks it. It means only that no quote from the passes was found on a page.
- This file does not rank the platforms and does not say that one is better than another.

## Result of the check

The passes gave 87 rows with a quote and a page for these six platforms. The check found the quote on the page in 84 rows.

| Platform | Rows tested | Quote found | Capabilities with a verified row (of 7) |
|---|---|---|---|
| Microsoft Azure AI Foundry Agent Service | 14 | 14 | 7 |
| Amazon Bedrock Agents and AgentCore | 14 | 14 | 7 |
| Google Vertex AI Agent Engine | 13 | 12 | 7 |
| Microsoft Copilot Studio | 12 | 12 | 7 |
| OpenAI Agents SDK and Responses API | 21 | 19 | 7 |
| LangGraph with LangSmith | 13 | 13 | 7 |

The passes also read one specification, the OpenTelemetry semantic conventions for generative AI. It is not a platform, so the table above leaves it out. The passes reported only `traces` for it. The check found the quote on the page in 2 of 2 rows. The section after the six platforms lists them.

A page that the script could not read in full (for example, a page that builds its text in the browser) gives no verified row, even if the page holds the quote.

## Microsoft Azure AI Foundry Agent Service

| Capability | Passes with a verified quote (of 2) | Documented mechanism (label, not checked) | Quote (verified on the page) | Page |
|---|---|---|---|---|
| `conversation` | 2 | Threads, runs, messages; Conversations API | "Create a thread once and append messages to it as users reply. The conversation history is maintained and managed automatically." | <https://learn.microsoft.com/en-us/azure/foundry-classic/agents/concepts/threads-runs-messages> |
| `conversation` | 2 | Responses API + Conversations API (REST/SDK) | "Create a conversation, then create multiple conversation items. Each conversation item corresponds to one response." | <https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/function-calling> |
| `traces` | 2 | Agent tracing; OpenTelemetry spans; Application Insights | "Traces address these challenges by allowing you to view the inputs and outputs of each primitive involved in a particular agent run" | <https://learn.microsoft.com/en-us/azure/foundry/observability/concepts/trace-agent-concept> |
| `traces` | 2 | OpenTelemetry tracing into Application Insights | "Tracing captures detailed telemetry - including latency, exceptions, prompt content, and retrieval operations" | <https://learn.microsoft.com/en-us/azure/foundry/observability/how-to/trace-agent-setup> |
| `tool_routing` | 2 | Function tools; function_call_output; MCP tools | "Define a function with its name, parameters, and description, and the agent's Foundry model can request your app to call it." | <https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/function-calling> |
| `tool_routing` | 2 | Function tools, OpenAPI tools, MCP tools | "Your app executes the function and returns the output. The agent then uses the result to continue the conversation" | <https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/function-calling> |
| `state_read` | 2 | Threads/runs/messages (classic) + Conversations API | "Threads are conversation sessions between an agent and a user. They store messages and automatically handle truncation" | <https://learn.microsoft.com/en-us/azure/foundry-classic/agents/concepts/threads-runs-messages> |
| `state_read` | 2 | Thread and message list APIs; optional Cosmos DB state | "Threads are conversation sessions between an agent and a user. They store messages and automatically handle truncation to fit content into a model's context." | <https://learn.microsoft.com/en-us/azure/foundry-classic/agents/concepts/threads-runs-messages> |
| `data_seeding` | 2 | File search: file upload + vector stores | "Upload the file and add it to a vector store." | <https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/file-search> |
| `data_seeding` | 2 | File search tool; vector stores; file upload | "Use the file search tool to ground Microsoft Foundry agents in your own data." | <https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/file-search> |
| `config_read` | 2 | Agent GET + version-history/YAML comparison + RBAC | "Access the version history for any agent, go to any specific version, and perform the following comparisons" | <https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/development-lifecycle> |
| `config_read` | 2 | GET /agents/{agent_name}; agent object properties | "To view your agent's current properties - identity, protocols, authorization, and endpoint configuration - run" | <https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/configure-agent> |
| `control` | 2 | version_selector pin; agent versions; delete_version | "As you iterate on your agent, versions are automatically snapshotted. Roll back to any previous version or compare changes between versions." | <https://learn.microsoft.com/en-us/azure/foundry/agents/overview> |
| `control` | 2 | Agent versioning/rollback, model swap, deployment start/stop, delete | "Roll back to any previous version or compare changes between versions" | <https://learn.microsoft.com/en-us/azure/foundry/agents/overview> |

Not verified: none. Every capability has at least one verified row. This does not mean that the platform fully supports the capability.

## Amazon Bedrock Agents and AgentCore

| Capability | Passes with a verified quote (of 2) | Documented mechanism (label, not checked) | Quote (verified on the page) | Page |
|---|---|---|---|---|
| `conversation` | 2 | InvokeAgent with sessionId; AgentCore: InvokeAgentRuntime with runtimeSessionId | "To continue the same conversation with an agent, use the same sessionId value in the request." | <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_InvokeAgent.html> |
| `conversation` | 2 | InvokeAgent API (agent-runtime) | "You send input to your agent by using InvokeAgent with the test alias, TSTALIASID, or a different alias pointing to a static version." | <https://docs.aws.amazon.com/bedrock/latest/userguide/agents-test.html> |
| `traces` | 2 | InvokeAgent enableTrace; AgentCore Observability | "AgentCore emits telemetry data in standardized OpenTelemetry (OTEL)-compatible format, enabling you to easily integrate it with your existing monitoring and observability stack." | <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html> |
| `traces` | 2 | enableTrace → TracePart (ModelInvocationInput, Observation); AgentCore: Observability OTEL spans | "Each response from an Amazon Bedrock agent is accompanied by a trace that details the steps being orchestrated by the agent." | <https://docs.aws.amazon.com/bedrock/latest/userguide/trace-events.html> |
| `tool_routing` | 2 | Action-group Lambda + RETURN_CONTROL; AgentCore: Gateway MCP tools | "You can define a Lambda function to program the business logic for an action group." | <https://docs.aws.amazon.com/bedrock/latest/userguide/agents-lambda.html> |
| `tool_routing` | 2 | Action groups with RETURN_CONTROL; ReturnControlPayload | "you can instead choose to return control to the agent developer by sending the information in the InvokeAgent response" | <https://docs.aws.amazon.com/bedrock/latest/userguide/agents-returncontrol.html> |
| `state_read` | 2 | sessionState sessionAttributes; agent memory (memoryId); AgentCore: Memory | "You can access the memory at any time to view the summarized version of the sessions that are stored in the memory." | <https://docs.aws.amazon.com/bedrock/latest/userguide/agents-memory.html> |
| `state_read` | 2 | SessionState sessionAttributes; AgentCore Memory | "The SessionState object contains information that can be maintained across turns" | <https://docs.aws.amazon.com/bedrock/latest/userguide/agents-session-state.html> |
| `data_seeding` | 2 | IngestKnowledgeBaseDocuments API; S3/custom data sources; AgentCore: Memory | "If you use the IngestKnowledgeBaseDocuments API instead, you can ingest up to 25 documents into your knowledge base." | <https://docs.aws.amazon.com/bedrock/latest/userguide/kb-direct-ingestion-add.html> |
| `data_seeding` | 2 | Amazon Bedrock Knowledge Bases; AgentCore Memory | "With Amazon Bedrock Knowledge Bases, you can integrate proprietary information into your generative-AI applications." | <https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base.html> |
| `config_read` | 2 | GetAgent (instruction, foundationModel, guardrailConfiguration); GetGuardrail | "Instructions that tell the agent what it should do and how it should interact with users." | <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_Agent.html> |
| `config_read` | 2 | GetAgent API | "Gets information about an agent." | <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_GetAgent.html> |
| `control` | 2 | UpdateAgentAlias; aliasInvocationState; versions and aliases | "To pause an agent, set the aliasInvocationState field to REJECT_INVOCATIONS." | <https://docs.aws.amazon.com/bedrock/latest/userguide/deploy-agent.html> |
| `control` | 2 | UpdateAgentAlias (pause, rollback, version switch); UpdateAgent+PrepareAgent; DeleteAgent | "you can change an alias to point to a previous version of your agent if there are changes that you need to revert quickly." | <https://docs.aws.amazon.com/bedrock/latest/userguide/deploy-agent.html> |

Not verified: none. Every capability has at least one verified row. This does not mean that the platform fully supports the capability.

## Google Vertex AI Agent Engine

| Capability | Passes with a verified quote (of 2) | Documented mechanism (label, not checked) | Quote (verified on the page) | Page |
|---|---|---|---|---|
| `conversation` | 2 | Agent Runtime run / run_sse API; Sessions | "the code for querying an agent is the same regardless of whether it is running locally or deployed remotely" | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-an-agent> |
| `conversation` | 2 | `stream_query` / `async_stream_query`; Sessions API | "Asynchronously stream responses to serve the user query." | <https://docs.cloud.google.com/python/docs/reference/vertexai/latest/vertexai.agent_engines.AsyncStreamQueryable> |
| `traces` | 2 | Agent traces (Cloud Trace); OpenTelemetry | "A trace is a factual, immutable record of the agent's behavior, including model inputs, responses, and tool calls." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/optimize/observability/traces> |
| `traces` | 2 | Cloud Trace (OpenTelemetry) spans for LLM/tool calls | "You can use traces to identify bottlenecks and understand interactions with large language models (LLMs) or tools." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/tracing> |
| `tool_routing` | 1 | Interactions API per-interaction `mcp_server` tool override | "Any tools or MCP servers specified in the interaction request body completely override the agent's preconfigured tools for the duration of that interaction turn." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/managed-agents/interact-with-agents> |
| `state_read` | 2 | Sessions API (ListEvents/AppendEvent/GetSession); Memory Bank fetch | "AppendEvent: As the user interacts with the agent, events (such as user messages, agent responses, tool actions) are uploaded to Sessions." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank> |
| `state_read` | 2 | Sessions API list events; Memory Bank fetch | "List events in a session associated with your Agent Platform instance." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/sessions/manage-with-api> |
| `data_seeding` | 1 | Memory Bank CreateMemory/GenerateMemories; RAG `ragFiles.import` | "This document shows how to create, upload, retrieve, and remove memories using API calls." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/api-quickstart> |
| `config_read` | 2 | `reasoningEngines.get` returns ReasoningEngineSpec | "You can list, get, update, and delete deployed agents using the console, SDK, or REST API." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/manage-deployed-agents> |
| `config_read` | 2 | ReasoningEngine get; runtimeRevisions get | "You can retrieve the details for a specific revision." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/manage-revisions-and-traffic> |
| `control` | 2 | Runtime revisions + traffic split; patch/delete | "Traffic splitting lets you test and gradually increase traffic to new revisions and split traffic between revisions for other purposes." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/manage-revisions-and-traffic> |
| `control` | 2 | runtimeRevisions; traffic_config (preview) | "You can then split traffic between the different active revisions." | <https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/manage-revisions-and-traffic> |

Not verified: none. Every capability has at least one verified row. This does not mean that the platform fully supports the capability.

## Microsoft Copilot Studio

| Capability | Passes with a verified quote (of 2) | Documented mechanism (label, not checked) | Quote (verified on the page) | Page |
|---|---|---|---|---|
| `conversation` | 2 | Direct Line API 3.0 (REST/WebSocket) | "You can enable your client application to communicate with your bot by using Direct Line API 3.0." | <https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-direct-line-3-0-api-reference> |
| `conversation` | 2 | Direct Line API; Bot Framework | "After retrieving the Direct Line token, you're ready to have a conversation with your Copilot Studio agent by using Direct Line." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-connect-bot-to-custom-application> |
| `traces` | 2 | Monitor analytics; session transcripts | "Session details and transcript information are available for the last 28 days." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-overview> |
| `traces` | 2 | Conversation transcript (Dataverse); activity trace; enhanced transcripts | "The Content field is a raw log of all the activities that users had with the agent." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-transcripts-powerapps> |
| `tool_routing` | 2 | HTTP Request node; REST API tool; MCP server; activity trace | "In the URL box, enter the URL for the API endpoint you want to call." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/authoring-http-node> |
| `tool_routing` | 2 | Azure Bot Service skills | "Azure Bot Service skills, a code-based way to extend the capabilities of an agent in the standard harness." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-fundamentals-publish-channels> |
| `state_read` | 2 | Dataverse transcripts; context variables | "You can download conversation transcripts a few minutes after the conversation times out." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-overview> |
| `state_read` | 2 | Dataverse Web API (conversationtranscript, bot tables) | "Contains the transcripts of conversations between participants such as customers, Virtual Agents or Human agents." | <https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/conversationtranscript> |
| `data_seeding` | 1 | Knowledge sources; Dataverse MCP create_record; non-production environments | "Populate Dataverse tables with new data for business records, configurations, or operational entities." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-dataverse> |
| `config_read` | 1 | Solution export (main prompt); Dataverse bot table | "The agent instructions (main prompt) are included." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/authoring-solutions-import-export> |
| `control` | 2 | Power Platform API (quarantine); solution import; model dropdown | "Mark an agent as quarantined, restricting its availability and interactions" | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/admin-api-quarantine> |
| `control` | 2 | Publish | "Each time you update your agent, you can publish it again from within Copilot Studio." | <https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-fundamentals-publish-channels> |

Not verified: none. Every capability has at least one verified row. This does not mean that the platform fully supports the capability.

## OpenAI Agents SDK and Responses API

| Capability | Passes with a verified quote (of 2) | Documented mechanism (label, not checked) | Quote (verified on the page) | Page |
|---|---|---|---|---|
| `conversation` | 2 | Responses API / Conversations API; SDK `Runner.run(session=, conversation_id=)` | "session: A session for automatic conversation history management." | <https://openai.github.io/openai-agents-python/ref/run/> |
| `conversation` | 2 | Responses API; Runner.run; Sessions | "Sessions store conversation history for a specific session, allowing agents to maintain context without requiring explicit manual memory management." | <https://openai.github.io/openai-agents-python/sessions/> |
| `traces` | 2 | Hosted Agents API `GET /v1/agents/sessions/{id}/traces` (OTLP); SDK generation/function spans | "The endpoint GET /v1/agents/sessions/{session_id}/traces returns a page of traces containing OpenTelemetry Protocol (OTLP) JSON." | <https://developers.openai.com/api/docs/guides/agents-api/tracing> |
| `traces` | 2 | Built-in tracing; traces and spans | "The Agents SDK includes built-in tracing, collecting a comprehensive record of events during an agent run: LLM generations, tool calls ..." | <https://openai.github.io/openai-agents-python/tracing/> |
| `tool_routing` | 2 | Function tools; function_call / function_call_output | "Execute code on the application side with input from the tool call" | <https://developers.openai.com/api/docs/guides/function-calling> |
| `tool_routing` | 2 | SDK `FunctionTool` wraps a harness-supplied function; Responses API function_call/function_call_output | "Function tools: Turn any Python function into a tool with automatic schema generation and Pydantic-powered validation." | <https://openai.github.io/openai-agents-python/> |
| `state_read` | 2 | Hosted Agents API `sessions.items.list` / `turns`; Conversations API items | "To inspect work that has already happened, retrieve the session's saved items" | <https://developers.openai.com/api/docs/guides/agents-api/observability> |
| `state_read` | 2 | Session.get_items; OpenAIConversationsSession | "Sessions support several operations for managing conversation history" | <https://openai.github.io/openai-agents-python/sessions/> |
| `data_seeding` | 1 | Vector stores; Files API; file_search tool | "By creating vector stores and uploading files to them, you can augment the models' inherent knowledge by giving them access to these knowledge bases" | <https://developers.openai.com/api/docs/guides/tools-file-search> |
| `config_read` | 2 | Agents API `sessions.retrieve`; prompts `id`/`version`/`variables`; pinned model versions | "Retrieve a session to read its status, agent configuration, environment, and required_actions." | <https://developers.openai.com/api/docs/guides/agents-api/sessions/manage> |
| `config_read` | 2 | Agent object (instructions, model, tools); RunConfig | "Agents, which are LLMs equipped with instructions and tools" | <https://openai.github.io/openai-agents-python/> |
| `control` | 2 | rollback: Agent Builder workflow version in API calls; prompt spec versioning | "You can create new versions or specify an older version in your API calls." | <https://developers.openai.com/api/docs/guides/agent-builder> |
| `control` | 2 | failure injection: Agents API tool_result sets success false; SDK ModelStep.raise_error() | "For an error, set success: false and supply an error message that the agent can use." | <https://developers.openai.com/api/docs/guides/agents-api/tools/functions> |
| `control` | 2 | version switch: `model` parameter with dated model snapshot IDs | "Snapshots let you lock in a specific version of the model so that performance and behavior remain consistent." | <https://developers.openai.com/api/docs/models/gpt-4.1> |
| `control` | 2 | version switch: Responses API model parameter; pinned model version IDs | "The best way to ensure consistent prompting behavior and model output is to use pinned model versions" | <https://developers.openai.com/api/reference/overview> |
| `control` | 2 | stop: Responses API cancel a background response; Agents SDK RunResultStreaming.cancel() | "Cancels a model response with the given ID." | <https://developers.openai.com/api/reference/resources/responses/methods/cancel> |
| `control` | 2 | stop: Responses API `POST /responses/{response_id}/cancel`; Agents SDK `result.cancel()` | "Cancels a model response with the given ID. Only responses created with the background parameter set to true can be cancelled." | <https://developers.openai.com/api/reference/resources/responses/methods/cancel/> |
| `control` | 2 | rollback: Skills API set default version pointer (default_version) | "Update the default version pointer for a skill." | <https://developers.openai.com/api/reference/resources/skills/methods/update> |
| `control` | 2 | failure injection: `ModelStep.raise_error()` in Agents SDK testing module | "Use ModelStep.raise_error() to fail one model call." | <https://openai.github.io/openai-agents-python/testing/> |

Not verified: none. Every capability has at least one verified row. This does not mean that the platform fully supports the capability.

## LangGraph with LangSmith

| Capability | Passes with a verified quote (of 2) | Documented mechanism (label, not checked) | Quote (verified on the page) | Page |
|---|---|---|---|---|
| `conversation` | 2 | Agent Server API (threads/runs); RemoteGraph | "Once deployed, agents work with Agent Server's execution model: assistants for configuration, threads for state, and runs for workloads." | <https://docs.langchain.com/langsmith/deployment> |
| `conversation` | 2 | Agent Server threads + runs; `/threads`, `/runs/stream`; SDK `client.runs.stream` | "A thread is a persistent conversation container that maintains state across multiple runs." | <https://docs.langchain.com/langsmith/use-threads> |
| `traces` | 2 | LangSmith tracing; traces and runs | "LangSmith Observability provides full visibility into your LLM application: from individual traces to production-wide performance metrics." | <https://docs.langchain.com/langsmith/observability> |
| `traces` | 2 | LangSmith REST API/SDK traces + runs; spans record llm/tool/retriever calls | "The LangSmith REST API provides programmatic access to tracing, datasets, experiments, annotations, and other LangSmith platform features." | <https://docs.langchain.com/langsmith/smith-api-ref> |
| `tool_routing` | 2 | Assistant config tools; graph nodes | "A single deployed graph can support multiple assistants, each with different configurations (e.g., prompts, models, tools)." | <https://docs.langchain.com/langsmith/assistants> |
| `tool_routing` | 2 | Tools are Python/JS callables; fake chat models script tool calls | "Under the hood, tools are callable functions with well-defined inputs and outputs that get passed to a chat model." | <https://docs.langchain.com/oss/python/langchain/tools> |
| `state_read` | 2 | Threads API (state) | "assistants for configuration, threads for state, and runs for workloads" | <https://docs.langchain.com/langsmith/deployment> |
| `state_read` | 2 | Thread `get_state`/`get_history`; Store `get_item` (long-term memory) | "To view the current state of a given thread, use the get_state method." | <https://docs.langchain.com/langsmith/use-threads> |
| `data_seeding` | 1 | Store `put`/`search`; thread supersteps prepopulate state | "Use the store.put method to save memories to the namespace in the store." | <https://docs.langchain.com/oss/python/langgraph/stores> |
| `config_read` | 2 | Assistants API (get, list versions) | "Assistants are an Agent Server concept that allow you to manage configurations (e.g., prompts, LLM selection, tools) separately from your graph's core logic." | <https://docs.langchain.com/langsmith/assistants> |
| `control` | 2 | Assistant versioning; promote or roll back versions | "Rolling back to a previous version is as simple as setting it as active." | <https://docs.langchain.com/langsmith/assistants> |
| `control` | 2 | Cancel run (interrupt/rollback); assistant `set_latest` version; cron jobs | "You can cancel a single run by ID or cancel multiple runs by thread or status." | <https://docs.langchain.com/langsmith/cancel-run> |

Not verified: none. Every capability has at least one verified row. This does not mean that the platform fully supports the capability.

## OpenTelemetry semantic conventions for generative AI

A trace standard, not a platform. The passes reported only `traces` for it.

| Capability | Passes with a verified quote (of 2) | Documented mechanism (label, not checked) | Quote (verified on the page) | Page |
|---|---|---|---|---|
| `traces` | 2 | GenAI semantic conventions (agent spans) | "This document defines semantic conventions for GenAI agent calls that are defined by this whitepaper." | <https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md> |
| `traces` | 2 | gen_ai.client.inference, invoke_agent, execute_tool, retrieval, embeddings spans; gen_ai.* attributes | "GenAI spans represent logical operations as observed by the caller." | <https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-spans.md> |

## Method, for a repeat

1. Take each row with a page address and a quote.
2. Fetch the raw page with a plain HTTP request. Do not use a browser. Fetch each address once.
3. Remove scripts, styles and tags. Decode HTML entities.
4. Make the page text and the quote lower case. Make quote marks and dashes plain. Remove all spaces.
5. Test whether the quote occurs in the page text.

The script and the replies of the two passes are not part of this repository.
