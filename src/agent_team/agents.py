"""Agent card definitions for the agent team."""

from akgentic.agent import AgentConfig
from akgentic.core import AgentCard
from akgentic.llm import ModelConfig, PromptTemplate
from akgentic.llm.config import RuntimeConfig, RunUsageLimits
from tools import tools

LLM_MODEL = "gpt-5.6-luna"

manager_card = AgentCard(
    description="Helpful manager coordinating team work",
    skills=["coordination", "delegation"],
    agent_class="akgentic.agent.BaseAgent",
    config=AgentConfig(
        name="@Manager",
        role="Manager",
        prompt=PromptTemplate(
            template="You are a helpful manager. Coordinate the team effectively.",
        ),
        model_cfg=ModelConfig(provider="openai", model=LLM_MODEL),
        run_usage_limits=RunUsageLimits(run_request_limit=20, total_tokens_limit=200000),
        runtime_cfg=RuntimeConfig(),
        tools=tools,
    ),
)

assistant_card = AgentCard(
    description="Helpful assistant providing support",
    skills=["research", "writing"],
    agent_class="akgentic.agent.BaseAgent",
    config=AgentConfig(
        name="@Assistant",
        role="Assistant",
        prompt=PromptTemplate(
            template="You are a helpful assistant. Provide clear and accurate information."
        ),
        model_cfg=ModelConfig(provider="openai", model=LLM_MODEL),
        tools=tools,
    ),
    can_be_hired=True,
)

expert_card = AgentCard(
    description="Helpful expert providing specialized knowledge",
    skills=["analysis", "problem-solving"],
    agent_class="akgentic.agent.BaseAgent",
    config=AgentConfig(
        name="@Expert",
        role="Expert",
        prompt=PromptTemplate(
            template="You are a helpful expert. Provide deep specialized knowledge."
        ),
        model_cfg=ModelConfig(provider="openai", model=LLM_MODEL),
        tools=tools,
    ),
    can_be_hired=True,
)

agent_cards = [manager_card, assistant_card, expert_card]
