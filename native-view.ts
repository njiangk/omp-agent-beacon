import type { NativeNode, NativeScreen } from "@oh-my-pi/pi-tui";

export interface BeaconAgent {
  name: string;
  model: string;
  state: string;
  detail: string;
  tools: number;
  calls: number;
  progress: number | null;
  role: "main" | "advisor" | "subagent";
  elapsed: number;
  lastTool: string;
  tokens: number;
  cost: number;
}

export interface BeaconReview {
  stamp: string;
  detail: string;
  active: boolean;
}

export interface BeaconView {
  main: BeaconAgent;
  advisor: BeaconAgent;
  agents: BeaconAgent[];
  reviews: BeaconReview[];
  waiting: string;
  logs: [string, string, string, string][];
  total: number;
  label: string;
  error: string;
  mode: string;
  paused: boolean;
  page: number;
}

const ROLE = { main: "warning", advisor: "accent", subagent: "info" } as const;

const span = (value: string, style = ""): { t: string; s?: string } =>
  style ? { t: value, s: style } : { t: value };

const line = (spans: { t: string; s?: string }[], wrap: "word" | "none" = "word"): NativeNode => ({
  k: "text", p: { spans, wrap },
});

function duration(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
}

const fmtTokens = (value: number): string =>
  value >= 1e6 ? `${(value / 1e6).toFixed(1)}M` : value >= 1e3 ? `${(value / 1e3).toFixed(1)}k` : String(Math.round(value));

const fmtCost = (value: number): string =>
  value <= 0 ? "" : value < 0.01 ? `$${value.toFixed(4)}` : value < 1 ? `$${value.toFixed(3)}` : `$${value.toFixed(2)}`;

function usageLine(agent: BeaconAgent, compact = false): NativeNode | null {
  const parts: string[] = [];
  if (agent.tokens > 0) parts.push(fmtTokens(agent.tokens) + (compact ? "" : " tok"));
  const cost = fmtCost(agent.cost);
  if (cost) parts.push(cost);
  return parts.length ? line([span(parts.join(" · "), "mono")]) : null;
}

function agentCard(agent: BeaconAgent, head: string, index = -1, extra: NativeNode[] = [], advisorExpanded = false): NativeNode {
  const tone = ROLE[agent.role];
  const status = agent.state === "done" ? "done" : agent.state === "error" ? "error"
    : ["running", "responding", "consulting"].includes(agent.state) ? "running" : "pending";
  const label = index >= 0 ? `${String(index).padStart(2, "0")} ${head}` : head;
  const advisor = agent.role === "advisor";
  const reviewStatus = agent.state === "done" ? "审查已结束" : agent.state === "error" ? "审查失败"
    : status === "running" ? "审查中" : agent.state === "not connected" ? "未连接" : "等待审查";
  const content: NativeNode[] = [
    line([span(agent.model, `${tone} strong`)]),
    ...(advisor ? [{ k: "badge", p: { text: reviewStatus, tone: agent.state === "done" ? "success"
      : agent.state === "error" ? "error" : "accent",
      title: agent.state === "done" ? "仅表示本次顾问咨询结束，不代表建议中的问题已解决。" : undefined } } as NativeNode] : []),
    ...(advisor && agent.detail ? [{ k: "button", key: "advisor-detail", p: {
      text: advisorExpanded ? "收起全文" : "展开全文", actions: { click: "advisor-detail" },
    } } as NativeNode] : []),
    { k: "text", key: "detail", p: { spans: [span(agent.detail, "muted")], wrap: "word",
      ...(advisor && !advisorExpanded ? { lines: 3, truncate: "end", max: { h: "3lines" } } : {}) } },
    ...extra,
  ];
  if (agent.progress !== null) {
    content.push({ k: "progress", p: { value: agent.progress, label: `${Math.round(agent.progress * 100)}%`, tone } });
  } else if (agent.role === "subagent") {
    const metric = agent.state === "running" && agent.lastTool ? agent.lastTool : `${agent.tools} tools`;
    content.push(line([span(metric, tone), span("  ·  ", "dim"), span(duration(agent.elapsed), "mono")]));
  }
  const usage = usageLine(agent);
  if (usage) content.push(usage);
  return { k: "card", key: `${agent.role}-${head}`, p: { head: label, ...(advisor ? {} : { status }), tone, shrink: 0, max: { w: 1 } }, c: content };
}

const arrow = (label: string): NativeNode => ({
  k: "col", p: { align: "center", gap: "xs" }, c: [line([span(`↓  ${label}`, "info")])],
});

export function beaconScreen(view?: BeaconView, advisorExpanded = false): NativeScreen {
  if (!view) return { role: "agent-beacon", main: [line([span("Starting Agent Beacon…", "muted")])], dock: [] };
  const spend = [view.main, view.advisor, ...view.agents].reduce(
    (sum, agent) => ({ tokens: sum.tokens + agent.tokens, cost: sum.cost + agent.cost }),
    { tokens: 0, cost: 0 },
  );
  const spendLine = spend.tokens || spend.cost
    ? line([
      span(`Σ ${fmtTokens(spend.tokens)} tok`, "mono"),
      ...(spend.cost ? [span("  ·  ", "dim"), span(fmtCost(spend.cost), "mono")] : []),
    ])
    : null;
  const pages = Math.max(1, Math.ceil(view.agents.length / 3));
  const page = view.page % pages;
  const agents = view.agents.slice(page * 3, page * 3 + 3);
  const reviews = view.reviews.slice(-4);
  const advisor: NativeNode = {
    k: "col", key: "advisor", p: { basis: 0.27, grow: 1, min: { w: "24ch" }, gap: "md" },
    c: [
      agentCard(view.advisor, "ADVISOR", -1, [], advisorExpanded),
      { k: "card", p: { head: "REVIEWS", meta: String(view.advisor.calls), tone: "accent", shrink: 0 }, c: reviews.length
        ? reviews.map(review => line([
          span(review.stamp, review.active ? "accent" : "dim"),
          span(review.active ? "  live  " : "  ", "accent"),
          span(review.detail, "muted"),
        ], "none"))
        : [line([span("No advisor review observed yet.", "dim")])] },
    ],
  };
  const main = agentCard(view.main, "MAIN SESSION", -1, view.waiting ? [line([span(view.waiting, "accent")])] : []);

  const flow: NativeNode = {
    k: "col", key: "flow", p: { basis: 0.69, grow: 3, min: { w: "32ch" }, gap: "md" },
    c: [
      main,
      arrow("delegate"),
      { k: "card", p: { head: "EXECUTION BUS", tone: "info" }, c: [
        line([span(view.label)]),
        line([span(`${view.total} events`, "info"), span("  ·  ", "dim"), span(`${view.main.tools} main tools`, "info")]),
        ...(spendLine ? [spendLine] : []),
      ] },
      arrow(`${view.agents.length} subagents · page ${page + 1}/${pages}`),
      agents.length ? { k: "row", p: { wrap: true, align: "stretch", gap: "sm" }, c: agents.map((agent, i) => {
        const card = agentCard(agent, agent.name, page * 3 + i + 1);
        return { ...card, p: { ...card.p, grow: 1, basis: 0.3, min: { w: "16ch" } } };
      }) }
        : { k: "card", p: { head: "SUBAGENTS", tone: "muted" }, c: [line([span("No delegated tasks in this session yet.", "dim")])] },
      arrow("results return"),
      { k: "card", p: { head: "BACK TO MAIN", tone: "warning" }, c: [line([span("Review + verify", "muted")])] },
    ],
  };
  return {
    role: "agent-beacon",
    main: [{
      k: "col", key: "beacon", p: { gap: "lg", grow: 1, max: { w: 1 } },
      c: [
        { k: "row", p: { wrap: true, justify: "between", gap: "sm" }, c: [
          { k: "col", p: { gap: "xs" }, c: [line([span("AGENT BEACON", "strong")]),
            line([span("会话执行图 · 工具活动 · 用量", "muted")])] },
          line([span("■ main", "warning"), span("   "), span("■ subagents", "info"), span("   "), span("■ advisor", "accent")], "none"),
          { k: "badge", p: { text: view.mode.toUpperCase(), tone: view.mode.startsWith("demo") ? "warning" : "success" } },
        ] },
        { k: "row", p: { wrap: true, align: "stretch", gap: "md", shrink: 0 }, c: [advisor, flow] },
        { k: "card", key: "log", p: { head: "SESSION LOG · LIVE", shrink: 0 }, c: view.logs.length
          ? [{ k: "col", p: { gap: "xs" }, c: view.logs.slice(-8).map(([stamp, name, detail, role]) => line([
            span(`${stamp}  `, "dim"),
            span(name.padEnd(12, " "), ROLE[role as keyof typeof ROLE] ?? "info"),
            span(detail, "muted"),
          ], "none")) }]
          : [line([span("Ready. Activity appears when this OMP session runs a task.", "dim")])] },
        ...(view.error ? [line([span(view.error, "error")])] : []),
      ],
    }],
    dock: [{ k: "row", p: { wrap: true, gap: "sm" }, c: [
      { k: "button", p: { text: "Back to chat", keys: ["escape"], actions: { click: "close" } } },
      { k: "button", p: { text: view.paused ? "Resume view" : "Freeze view", keys: ["space"], actions: { click: "pause" } } },
      { k: "button", p: { text: "Next agents", keys: ["tab"], actions: { click: "next" } } },
    ] }],
  };
}

/** The custom-UI wrapper exposes describe(), even on hosts without describeScreen forwarding. */
export function beaconOverlay(screen: NativeScreen): NativeNode {
  return {
    k: "col", p: { gap: "md", grow: 1, min: { h: 0.88 }, max: { w: 1, h: 0.94 } },
    c: [
      { k: "scroll", p: { max: { h: 0.84 }, grow: 1, shrink: 1 }, c: screen.main },
      { k: "row", p: { shrink: 0 }, c: screen.dock },
    ],
  };
}
