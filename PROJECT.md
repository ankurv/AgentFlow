# 🚀 AgentFlow: The AI Architecture Design Studio

## What is AgentFlow?
AgentFlow is a premium, interactive **AI Architect Dashboard**. Instead of letting AI blindly generate codebases, AgentFlow focuses exclusively on the critical initial step of software development: **Architecture, Strategy, and Planning**. 

You feed it an idea, and it orchestrates a massive, multi-model debate among specialized AI personas to produce a crisp, structured implementation plan. You can then export this plan and hand it to any AI builder (like Cursor, Codex, or Windsurf) to flawlessly write the code.

---

## 🏗️ Core Architecture & Implemented Features

### 1. The Dynamic Agent Factory (Virtual Company)
Gone are the days of manually configuring 15 different agents. The AgentFlow backend acts as a dynamic factory. By providing just a single Base Provider API Key, the Orchestrator instantly spawns an entire **15-person Virtual Company** containing highly specialized personas:
- **Architect Alpha & Beta**: Two distinct brains forced to propose competing architectures and debate trade-offs.
- **Red Team**: Hunts for edge cases, security flaws, and race conditions.
- **UX Simplifier**: Fiercely advocates for the external user, fighting to simplify complex UI flows.
- **Product Manager**: Enforces strict MVP constraints and fights scope-creep ("YAGNI").
- **Cloud & Data Architects**: Obsess over schema design, DB normalization, and AWS/GCP infrastructure.
- **Sales & Marketing (Alpha/Beta)**: Competing strategists pitching aggressive viral loops vs. calculated B2B sales funnels.
- **Silent Researcher**: Reads your existing codebase in the background to ensure the other models don't hallucinate APIs.

### 2. Cross-Model Debates (Model Agnostic)
AgentFlow natively supports cross-model intelligence. If you provide multiple API keys (e.g., Claude, OpenAI, Gemini), the backend distributes the 15 specialized personas across your keys in a round-robin format. 
This means you can watch **Claude** (playing the UX Simplifier) natively debate **OpenAI** (playing the Red Team) to build the ultimate architecture.

### 3. Exhaustive Tree-Based Planning
The Orchestrator forces the Virtual Company through a rigorous two-tiered design phase:
1. **High-Level Strategy**: Debating the architecture, tech stack, and scalability, outputting a beautiful `DESIGN.md` complete with Mermaid.js flowcharts.
2. **Deep-Dive Implementation Tree**: For *every single* sub-item in the high-level plan, the Orchestrator forces the experts to debate exactly how to implement and test it. The final output is an exhaustive, deeply nested markdown tree inside `PLAN.md`.

### 4. Enterprise API Routing
AgentFlow supports enterprise-grade connections out of the box:
- **Foundry / Custom Endpoints**: Configurable `base_url` for OpenAI-compatible endpoints.
- **AWS Bedrock & GCP Vertex AI**: Native Anthropic SDK overrides to securely route Claude traffic through enterprise cloud platforms without manual proxying.

### 5. Developer-Focused UI/UX
- **Monaco Editor Integration**: The internal text editor is powered by Monaco (the engine behind VS Code) for a premium, syntax-highlighted project viewing experience.
- **Streamlined Debate Controls**: Complex agent settings were replaced with a simple "Debate Level" slider to control the depth of the plan.
- **Max Token Failsafe**: A hard Token Limit input allows the Orchestrator to monitor `input + output` tokens across all 15 agents and pull the emergency brake if a debate spirals, saving API costs.
- **1-Click Context Export**: A dedicated button instantly bundles your `DESIGN.md` and `PLAN.md` into your clipboard, perfectly formatted to be pasted into a coding agent.
