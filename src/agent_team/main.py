"""Agent Team Coordination Example

This example demonstrates the core capabilities of the akgentic framework:

**Multi-Agent Team Coordination**
Creates a hierarchical team structure with a Manager coordinating Assistant and Expert
agents. The Orchestrator monitors all interactions and maintains team state, providing
centralized telemetry without coupling agents to each other.

**Agent Cards — Dynamic Team Composition**
Uses AgentCard to define available roles with skills and configurations. Cards are
registered with the Orchestrator, creating a catalog of roles that can be instantiated
dynamically. Each card encapsulates role definition, capabilities, and whether it can be hired.

**Asynchronous Message Flow & Autonomous Communication**
Agents communicate via asynchronous messages (AgentMessage). When an agent receives a message,
it produces a StructuredOutput with a list of Request objects, routing messages to other agents
by @name or by role (triggering dynamic hiring). Each agent in the chain can further delegate
to others, enabling complex multi-step workflows without central coordination. The HumanProxy
initiates requests that flow through the system, with agents processing and delegating
independently. No blocking calls—everything flows through the actor system's message passing,
allowing agents to work concurrently.

**Dynamic Hiring/Firing**
Agents can dynamically create child agents through the Orchestrator proxy using
`createActor()`. This enables runtime team scaling—hire new specialists when needed,
shut them down when done. The Manager can spawn Assistant and Expert agents on demand,
and all team changes are tracked by the Orchestrator.

**Interactive Chat Loop**
Demonstrates human-in-the-loop interaction where users can send messages to the team.
Messages can be routed to specific agents using `@AgentName` syntax, or sent to the
Manager for delegation.

Usage:
    python src/agent_team/main.py

    Then type messages to interact with the team:
    - Direct message: "Help me with a task"  (routes to Manager)
    - Routed message: "@Assistant What is the weather?"  (routes to Assistant)
    - Exit: "exit"
"""

import logging
import time
import uuid
from collections import defaultdict

import logfire
from agents import agent_cards, assistant_card, expert_card, manager_card
from akgentic.agent import AgentMessage, BaseAgent, HumanProxy
from akgentic.core import ActorSystem, BaseConfig, EventSubscriber, Orchestrator
from akgentic.core.messages import Message
from akgentic.core.messages.orchestrator import EventMessage, SentMessage
from akgentic.llm import LlmUsageEvent, ToolCallEvent, aggregate_usage

logging.basicConfig(level=logging.ERROR, format="%(name)s - %(levelname)s - %(message)s")


class MessagePrinter(EventSubscriber):
    """Prints messages as they flow through the orchestrator.

    Subscribes to orchestrator events and prints relevant message traffic
    to provide visibility into agent interactions. Collects LlmUsageEvent
    per agent for on-demand cost reporting via get_usage_report().

    V1 equivalent: Controller with PrintModule
    """

    EXCLUDED_ROLES = {"Orchestrator", "Human", "human"}

    def __init__(self) -> None:
        self._usage_events: dict[str, list[LlmUsageEvent]] = defaultdict(list)

    def set_restoring(self, team_id: uuid.UUID, restoring: bool) -> None:  # noqa: FBT001
        pass

    def on_stop(self, team_id: uuid.UUID) -> None:
        pass

    def on_stop_request(self, team_id: uuid.UUID) -> None:
        pass

    def on_message(self, message: Message) -> None:
        assert message.sender is not None
        sender = message.sender.name

        if isinstance(message, SentMessage):
            self.handle_sent_message(message, sender)
        elif isinstance(message, EventMessage):
            if isinstance(message.event, ToolCallEvent):
                self.handle_tool_call_event(message, sender)
            elif isinstance(message.event, LlmUsageEvent):
                if message.sender.role not in self.EXCLUDED_ROLES:
                    self._usage_events[sender].append(message.event)

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        """Format token count with European thousands separator (dot)."""
        return f"{n:,}".replace(",", ".")

    @staticmethod
    def _fmt_cost(c: float) -> str:
        """Format cost in European style (comma decimal). Show — when zero."""
        if c == 0.0:
            return "       —"
        formatted = f"{c:,.4f}".replace(",", " ").replace(".", ",").replace(" ", ".")
        return f"${formatted}"

    def get_usage_report(self) -> str:
        """Build a usage report across all tracked agents using aggregate_usage()."""
        lines = [
            "\n  LLM Usage Report",
            "  " + "=" * 80,
            f"  {'Agent':<20} {'in':>10}  {'out':>10}  {'reqs':>4}  {'cost':>10}",
            "  " + "-" * 80,
        ]

        total_input = 0
        total_output = 0
        total_cost = 0.0
        total_requests = 0

        for name in sorted(self._usage_events):
            summary = aggregate_usage(self._usage_events[name])
            total_input += summary.total_input_tokens
            total_output += summary.total_output_tokens
            total_cost += summary.total_cost_usd
            total_requests += summary.total_requests
            lines.append(
                f"  {name:<20} "
                f"{self._fmt_tokens(summary.total_input_tokens):>10}  "
                f"{self._fmt_tokens(summary.total_output_tokens):>10}  "
                f"{summary.total_requests:>4}  "
                f"{self._fmt_cost(summary.total_cost_usd):>10}"
            )

        lines.append("  " + "-" * 80)
        lines.append(
            f"  {'TOTAL':<20} "
            f"{self._fmt_tokens(total_input):>10}  "
            f"{self._fmt_tokens(total_output):>10}  "
            f"{total_requests:>4}  "
            f"{self._fmt_cost(total_cost):>10}"
        )
        return "\n".join(lines)

    def handle_tool_call_event(self, message: EventMessage, sender: str) -> None:
        assert isinstance(message.event, ToolCallEvent)
        event = message.event
        print("-" * 100)
        print(f"[{sender}] -> {event.tool_name} - {event.arguments}\n")

    def handle_sent_message(self, message: SentMessage, sender: str) -> None:
        msg = message.message
        recipient = message.recipient.name

        # Print any message that has a content attribute
        if hasattr(msg, "content"):
            content = getattr(msg, "content", "")
            type = getattr(msg, "type", "")
            print("-" * 100)
            print(f"[{sender}] -> {msg.__class__.__name__}({type}) [{recipient}]: \n{content}\n")


def main() -> None:
    """Run the simple team example with v2 APIs."""

    ## The logfire traces will now appear in the Logfire web UI as long as
    ## you have the LOGFIRE_TOKEN environment variable set.
    logfire.configure(console=False)

    # 1. Create ActorSystem
    actor_system = ActorSystem()

    # 2. Create Orchestrator
    orchestrator_addr = actor_system.createActor(
        Orchestrator, config=BaseConfig(name="@Orchestrator", role="Orchestrator")
    )
    orchestrator_proxy = actor_system.proxy_ask(orchestrator_addr, Orchestrator)

    # 3. Subscribe to orchestrator events
    printer = MessagePrinter()
    orchestrator_proxy.subscribe(printer)

    # 4. Register agent cards with orchestrator
    orchestrator_proxy.register_agent_profiles(agent_cards)

    # 5. Create HumanProxy
    human_config = BaseConfig(name="@Human", role="Human")
    human_addr = orchestrator_proxy.createActor(HumanProxy, config=human_config)
    human_proxy = actor_system.proxy_tell(human_addr, HumanProxy)

    # 6. Create Manager agent
    manager_addr = orchestrator_proxy.createActor(BaseAgent, config=manager_card.get_config_copy())
    manager_proxy = actor_system.proxy_ask(manager_addr, BaseAgent)

    # 7. Create the team from the manager
    manager_proxy.createActor(BaseAgent, config=assistant_card.get_config_copy())
    manager_proxy.createActor(BaseAgent, config=expert_card.get_config_copy())

    # 8. Wait for actors to initialize
    time.sleep(0.3)

    print()

    # 9. Interactive chat loop
    def print_help() -> None:
        print("Available commands:")
        print("  /exit        - Exit the chat loop")
        print("  /team        - Show team roster")
        print("  /roles       - Show available roles")
        print("  /planning    - Show current team planning")
        print("  /task <id>   - Show a specific task")
        print("  /hire <role> - Hire a new team member")
        print("  /fire <name> - Fire a team member")
        print("  /usage       - Show LLM usage and cost per agent")
        print("  /help        - Show this help message")
        print()

    print(
        "\nType your message (start the message with @{agent_name} to route to specific agent, "
        "'exit' to quit or '/help' for help):"
    )

    print("-" * 100)

    # Friendly slash aliases → real CommandRegistry command names dispatched by the agent
    # (ADR-028). /help and /usage are handled locally by this CLI; every other "/" command
    # is sent to the manager as a normal message for its CommandRegistry to dispatch.
    command_aliases = {
        "team": "team_members",
        "roles": "team_roles",
        "planning": "planning_summary",
        "task": "get_planning_task",
        "hire": "hire_member",
        "fire": "fire_member",
    }

    while True:
        user_input = input("")
        print()

        if user_input.strip().lower() in ["exit", "/exit"]:
            print("Exiting chat loop.")
            break

        if user_input.strip() == "":
            continue

        if user_input.startswith("/"):
            # /help and /usage are local; every other "/" command is sent to the manager
            # as a normal message and dispatched by its CommandRegistry (ADR-028). The
            # result comes back on the event stream and is rendered by the printer.
            parts = user_input.split(" ", 1)
            command = parts[0][1:].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""
            if command == "help":
                print_help()
                continue
            if command == "usage":
                print(printer.get_usage_report())
                print()
                continue
            real = command_aliases.get(command, command)
            text = f"/{real} {arg}".rstrip()
            human_proxy.send(manager_addr, AgentMessage(content=text))
            continue

        if user_input.startswith("@"):
            # Route to specific agent
            target_name = user_input.split(" ")[0]
            actor_addr = orchestrator_proxy.get_team_member(target_name)
            if actor_addr is None:
                print(f"Error: Agent {target_name} not found")
                continue
            human_proxy.send(actor_addr, AgentMessage(content=user_input))
            continue

        human_proxy.send(manager_addr, AgentMessage(content=user_input))

    # 10. Shutdown
    actor_system.shutdown()


if __name__ == "__main__":
    main()
