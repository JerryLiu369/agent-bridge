#!/usr/bin/env node
/**
 * Pi-Bridge Server
 * Bridges Python ↔ Pi Agent SDK via JSONL stdin/stdout.
 *
 * Protocol:
 *   stdin:  JSON lines (init msg, then commands)
 *   stdout: JSON lines (ready, events, responses)
 */

import { createInterface } from 'readline';
import { execFileSync } from 'child_process';

// ---------------------------------------------------------------------------
// Resolve Pi SDK from global install (via NODE_PATH or explicit path)
// ---------------------------------------------------------------------------

const PI_AGENT_PACKAGE = '@earendil-works/pi-coding-agent';

function discoverPiAgentBase() {
    if (process.env.PI_AGENT_BASE) return process.env.PI_AGENT_BASE;
    try {
        return `${execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim()}/${PI_AGENT_PACKAGE}`;
    } catch {
        return '';
    }
}

const PI_AGENT_BASE = discoverPiAgentBase();
const PI_AI_BASE = `${PI_AGENT_BASE}/node_modules/@earendil-works/pi-ai`;
const TYPEBOX_BASE = `${PI_AGENT_BASE}/node_modules/typebox`;

const {
    createAgentSession,
    AuthStorage,
    SessionManager,
    defineTool,
} = await import(`${PI_AGENT_BASE}/dist/index.js`);

const { getModel } = await import(`${PI_AI_BASE}/dist/index.js`);

const { Type } = await import(`${TYPEBOX_BASE}/build/index.mjs`);

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

/** Write a JSON line to stdout */
const emit = (obj) => process.stdout.write(JSON.stringify(obj) + '\n');

/** Map base_url hostname → Pi provider name */
function resolveProvider(baseUrl) {
    try {
        const host = new URL(baseUrl).hostname;
        const builtins = {
            'api.anthropic.com': 'anthropic',
            'api.openai.com': 'openai',
            'api.deepseek.com': 'deepseek',
            'api.groq.com': 'groq',
            'api.x.ai': 'xai',
        };
        return builtins[host] ?? host;
    } catch {
        return 'anthropic';
    }
}

/** Map api_format string → Pi SDK api field */
const FORMAT_TO_API = {
    completion: 'openai-completions',
    response: 'openai-responses',
    anthropic: 'anthropic-messages',
};

/** Valid thinking levels */
const VALID_LEVELS = new Set(['off', 'minimal', 'low', 'medium', 'high', 'xhigh']);

// ---------------------------------------------------------------------------
// JSON Schema → TypeBox conversion
// ---------------------------------------------------------------------------

function jsonSchemaToTypebox(schema) {
    const convert = (s) => {
        if (!s || typeof s !== 'object') {
            throw new Error(`Invalid schema node: ${JSON.stringify(s)}`);
        }

        // Reject unsupported composite keywords
        for (const k of ['anyOf', 'oneOf', 'allOf', '$ref']) {
            if (k in s) throw new Error(`Unsupported JSON Schema keyword: ${k}`);
        }

        const { type, description, enum: enumValues, items, properties, required, ...rest } = s;
        const opts = {};
        if (description) opts.description = description;

        // Enum on string type
        if (enumValues && Array.isArray(enumValues)) {
            return Type.Union(enumValues.map(v => Type.Literal(v)), opts);
        }

        switch (type) {
            case 'string':  return Type.String(opts);
            case 'number':  return Type.Number(opts);
            case 'integer': return Type.Integer(opts);
            case 'boolean': return Type.Boolean(opts);
            case 'null':    return Type.Null(opts);
            case 'array': {
                const itemSchema = items ? convert(items) : Type.Unknown();
                return Type.Array(itemSchema, opts);
            }
            case 'object': {
                const requiredSet = new Set(required ?? []);
                const props = {};
                for (const [k, v] of Object.entries(properties ?? {})) {
                    const converted = convert(v);
                    props[k] = requiredSet.has(k) ? converted : Type.Optional(converted);
                }
                return Type.Object(props, opts);
            }
            default:
                return Type.Unknown(opts);
        }
    };

    return convert(schema);
}

// ---------------------------------------------------------------------------
// Build a Pi Model object from our init params
// ---------------------------------------------------------------------------

function buildModel(providerName, modelConfig, baseUrl) {
    const apiField = FORMAT_TO_API[modelConfig.api_format];
    if (!apiField) {
        throw new Error(`Unsupported api_format: "${modelConfig.api_format}". Valid values: completion, response, anthropic`);
    }

    const normalizedBaseUrl = apiField.startsWith('openai-')
        ? normalizeOpenAIBaseUrl(baseUrl)
        : baseUrl;

    // Try known model first
    let model = getModel(providerName, modelConfig.name);

    if (model) {
        // Override api and baseUrl from user config
        model = { ...model, api: apiField, baseUrl: normalizedBaseUrl, provider: providerName };
    } else {
        // Construct custom model object
        model = {
            id: modelConfig.name,
            name: modelConfig.name,
            api: apiField,
            provider: providerName,
            baseUrl: normalizedBaseUrl,
            reasoning: ['high', 'xhigh', 'medium', 'low', 'minimal'].includes(modelConfig.thinking),
            input: ['text'],
            cost: { input: 0, output: 0 },
            contextWindow: 200000,
            maxTokens: 16384,
        };
    }

    return model;
}

function normalizeOpenAIBaseUrl(baseUrl) {
    const trimmed = baseUrl.replace(/\/+$/, '');
    return trimmed.endsWith('/v1') ? trimmed : `${trimmed}/v1`;
}

// ---------------------------------------------------------------------------
// Async line reader
// ---------------------------------------------------------------------------

async function* readLines() {
    const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
    for await (const line of rl) {
        const trimmed = line.trim();
        if (trimmed) yield trimmed;
    }
}

const lines = readLines();

// ---------------------------------------------------------------------------
// Read and validate init message
// ---------------------------------------------------------------------------

const initLine = await lines.next();
if (initLine.done) {
    emit({ type: 'error', message: 'No init message received on stdin' });
    process.exit(1);
}

let initMsg;
try {
    initMsg = JSON.parse(initLine.value);
} catch (e) {
    emit({ type: 'error', message: `Invalid init JSON: ${e.message}` });
    process.exit(1);
}

if (initMsg.type !== 'init') {
    emit({ type: 'error', message: `Expected init message, got type: "${initMsg.type}"` });
    process.exit(1);
}

const {
    provider: providerConfig,
    model: modelConfig,
    cwd = process.cwd(),
    system_prompt = '',
    tools: rawToolNames = undefined,
    custom_tools: customToolDefs = [],
    persist = false,
} = initMsg;

const toolNames = Array.isArray(rawToolNames) ? rawToolNames : undefined;

// Validate thinking level
const thinkingVal = modelConfig.thinking ?? null;
if (thinkingVal !== null && !VALID_LEVELS.has(thinkingVal)) {
    emit({ type: 'error', message: `Unsupported thinking value: "${thinkingVal}". Valid values: ${[...VALID_LEVELS].join(', ')}` });
    process.exit(1);
}

// Build auth storage
const providerName = resolveProvider(providerConfig.base_url);
const authStorage = AuthStorage.inMemory();
if (providerConfig.api_key) {
    authStorage.setRuntimeApiKey(providerName, providerConfig.api_key);
}

// Build model
let piModel;
try {
    piModel = buildModel(providerName, modelConfig, providerConfig.base_url);
} catch (e) {
    emit({ type: 'error', message: e.message });
    process.exit(1);
}

// Pending custom tool resolvers: toolCallId → { resolve, reject }
const pendingTools = new Map();

// ---------------------------------------------------------------------------
// Build custom tool definitions
// ---------------------------------------------------------------------------

const customTools = [];
for (const toolDef of customToolDefs) {
    let parameters;
    try {
        parameters = jsonSchemaToTypebox(toolDef.parameters);
    } catch (e) {
        emit({ type: 'error', message: `Custom tool "${toolDef.name}" schema error: ${e.message}` });
        process.exit(1);
    }

    const toolOptions = {
        name: toolDef.name,
        label: toolDef.name,
        description: toolDef.description,
        parameters,
        execute: async (toolCallId, params) => {
            // Forward call to Python
            emit({ type: 'tool_request', id: toolCallId, tool: toolDef.name, args: params });

            // Await Python's tool_result
            const result = await new Promise((resolve, reject) => {
                const timer = setTimeout(() => {
                    pendingTools.delete(toolCallId);
                    reject(new Error(`Tool call "${toolCallId}" timed out after 300s`));
                }, 300_000);

                pendingTools.set(toolCallId, {
                    resolve: (v) => { clearTimeout(timer); resolve(v); },
                    reject: (e) => { clearTimeout(timer); reject(e); },
                });
            });

            const contentStr = typeof result.content === 'string'
                ? result.content
                : JSON.stringify(result.content);

            return {
                content: [{ type: 'text', text: contentStr }],
                details: null,
                isError: result.is_error ?? false,
            };
        },
    };

    if (toolDef.prompt_snippet) {
        toolOptions.promptSnippet = toolDef.prompt_snippet;
    }
    if (Array.isArray(toolDef.prompt_guidelines) && toolDef.prompt_guidelines.length > 0) {
        toolOptions.promptGuidelines = toolDef.prompt_guidelines;
    }

    customTools.push(defineTool(toolOptions));
}

// ---------------------------------------------------------------------------
// Create agent session
// ---------------------------------------------------------------------------

const sessionManager = persist ? SessionManager.create(cwd) : SessionManager.inMemory();

let session;
try {
    const thinkingLevel = (!thinkingVal || thinkingVal === 'off') ? undefined : thinkingVal;

    // Resolve tools allowlist:
    //   toolNames=undefined → use Pi defaults (built-ins active)
    //   toolNames=[]        → disable built-in tools; custom tools still active (noTools:"builtin")
    //   toolNames=[...]     → these built-in names + all custom tool names
    const customToolNames = customToolDefs.map(t => t.name);
    let sessionToolsOpts = {};
    if (toolNames !== undefined) {
        if (toolNames.length === 0 && customTools.length === 0) {
            sessionToolsOpts = { noTools: 'all' };
        } else if (toolNames.length === 0) {
            sessionToolsOpts = { noTools: 'builtin' };
        } else {
            // Explicit list: include named built-ins + all custom tools
            sessionToolsOpts = { tools: [...toolNames, ...customToolNames] };
        }
    }

    const result = await createAgentSession({
        cwd,
        authStorage,
        model: piModel,
        thinkingLevel,
        customTools: customTools.length > 0 ? customTools : undefined,
        sessionManager,
        ...sessionToolsOpts,
    });
    session = result.session;
} catch (e) {
    emit({ type: 'error', message: `Failed to create session: ${e.message}` });
    process.exit(1);
}

// Override system prompt if provided
if (system_prompt) {
    try {
        session.state.systemPrompt = system_prompt;
    } catch {
        // Best-effort; ignore if not settable
    }
}

// ---------------------------------------------------------------------------
// Subscribe to session events
// ---------------------------------------------------------------------------

session.subscribe((event) => {
    switch (event.type) {
        case 'message_update': {
            const ame = event.assistantMessageEvent;
            if (ame.type === 'text_delta') {
                emit({ type: 'text_delta', delta: ame.delta });
            } else if (ame.type === 'thinking_delta') {
                emit({ type: 'thinking_delta', delta: ame.delta });
            }
            break;
        }

        case 'tool_execution_start':
            emit({
                type: 'tool_call',
                tool_call_id: event.toolCallId,
                tool_name: event.toolName,
                arguments: event.args ?? {},
            });
            break;

        case 'tool_execution_end': {
            // Serialize result to string
            let content = '';
            if (event.result) {
                if (typeof event.result === 'string') {
                    content = event.result;
                } else if (Array.isArray(event.result?.content)) {
                    content = event.result.content
                        .filter(c => c.type === 'text')
                        .map(c => c.text)
                        .join('');
                } else {
                    content = JSON.stringify(event.result);
                }
            }
            emit({
                type: 'tool_result',
                tool_call_id: event.toolCallId,
                tool_name: event.toolName,
                content,
                is_error: event.isError,
            });
            break;
        }

        case 'turn_end': {
            // turn_end carries the AssistantMessage for this LLM inference.
            // Each turn_end = one inference, so emit usage here (skipping
            // failure messages where usage is unreliable).
            const msg = event.message;
            const stopReason = msg?.stopReason;
            if (msg?.role === 'assistant' && msg?.usage
                    && stopReason !== 'error' && stopReason !== 'aborted') {
                emit({
                    type: 'token_usage',
                    input_tokens: msg.usage.input ?? 0,
                    output_tokens: msg.usage.output ?? 0,
                    cached_input_tokens: msg.usage.cacheRead ?? 0,
                });
            }
            emit({ type: 'turn_end' });
            break;
        }

        case 'agent_end': {
            const msgs = event.messages ?? session.messages;
            const lastAssistant = [...msgs].reverse().find(m => m.role === 'assistant');
            const stopReason = lastAssistant?.stopReason ?? 'stop';
            emit({ type: 'agent_end', stop_reason: stopReason });
            break;
        }

        // Silently consume internal events
        case 'agent_start':
        case 'message_start':
        case 'message_end':
        case 'compaction_start':
        case 'compaction_end':
        case 'auto_retry_start':
        case 'auto_retry_end':
        case 'queue_update':
        case 'session_info_changed':
        case 'thinking_level_changed':
        case 'tool_execution_update':
            break;

        case 'turn_start':
            emit({ type: 'turn_start' });
            break;

        default:
            break;
    }
});

// ---------------------------------------------------------------------------
// Signal ready
// ---------------------------------------------------------------------------

emit({ type: 'ready' });

// ---------------------------------------------------------------------------
// Command loop
// ---------------------------------------------------------------------------

for await (const line of lines) {
    let cmd;
    try {
        cmd = JSON.parse(line);
    } catch (e) {
        emit({ type: 'error', message: `Invalid command JSON: ${e.message}` });
        continue;
    }

    switch (cmd.type) {
        case 'prompt':
            // Fire and forget — events stream via subscribe
            session.prompt(cmd.message).catch((e) => {
                emit({ type: 'error', message: `Prompt error: ${e.message}` });
                // Emit a synthetic agent_end so Python's send() can unblock
                emit({ type: 'agent_end', stop_reason: 'error' });
            });
            break;

        case 'tool_result': {
            const pending = pendingTools.get(cmd.id);
            if (pending) {
                pendingTools.delete(cmd.id);
                pending.resolve({ content: cmd.content ?? '', is_error: cmd.is_error ?? false });
            }
            break;
        }

        case 'tool_error': {
            const pending = pendingTools.get(cmd.id);
            if (pending) {
                pendingTools.delete(cmd.id);
                pending.reject(new Error(cmd.message ?? 'Tool execution error'));
            }
            break;
        }

        case 'get_messages': {
            const messages = session.messages.map((m) => {
                if (m.role === 'assistant') {
                    return {
                        role: 'assistant',
                        content: (m.content ?? []).map((c) => {
                            if (c.type === 'text') return { type: 'text', text: c.text };
                            if (c.type === 'thinking') return { type: 'thinking', thinking: c.thinking };
                            if (c.type === 'toolCall') return { type: 'tool_call', name: c.name, arguments: c.arguments };
                            return { type: c.type };
                        }),
                        stop_reason: m.stopReason,
                        error_message: m.errorMessage,
                    };
                }
                if (m.role === 'user') {
                    const content = Array.isArray(m.content)
                        ? m.content.filter(c => c.type === 'text').map(c => c.text).join('')
                        : String(m.content ?? '');
                    return { role: 'user', content };
                }
                if (m.role === 'toolResult') {
                    return {
                        role: 'tool_result',
                        tool_call_id: m.toolCallId,
                        tool_name: m.toolName,
                        content: (m.content ?? []).filter(c => c.type === 'text').map(c => c.text).join(''),
                        is_error: m.isError,
                    };
                }
                return { role: m.role };
            });
            emit({ type: 'response', command: 'get_messages', success: true, data: { messages } });
            break;
        }

        case 'get_state': {
            const s = session.state;
            emit({
                type: 'response',
                command: 'get_state',
                success: true,
                data: {
                    message_count: s.messages?.length ?? 0,
                    is_streaming: s.isStreaming,
                    model: s.model ? { provider: s.model.provider, id: s.model.id } : null,
                    thinking_level: s.thinkingLevel,
                },
            });
            break;
        }

        case 'set_model': {
            const newProviderName = resolveProvider(cmd.provider.base_url);
            if (cmd.provider.api_key) {
                authStorage.setRuntimeApiKey(newProviderName, cmd.provider.api_key);
            }
            let newModel;
            try {
                newModel = buildModel(newProviderName, cmd.model, cmd.provider.base_url);
            } catch (e) {
                emit({ type: 'error', message: e.message });
                break;
            }
            session.setModel(newModel).catch((e) => {
                emit({ type: 'error', message: `set_model error: ${e.message}` });
            });
            break;
        }

        case 'set_thinking_level': {
            const level = cmd.level;
            if (!VALID_LEVELS.has(level)) {
                emit({ type: 'error', message: `Invalid thinking level: "${level}". Valid values: ${[...VALID_LEVELS].join(', ')}` });
                break;
            }
            session.setThinkingLevel(level);
            break;
        }

        case 'abort':
            session.abort().catch(() => {});
            break;

        case 'shutdown':
            process.exit(0);
            break;

        default:
            emit({ type: 'error', message: `Unknown command type: "${cmd.type}"` });
    }
}
