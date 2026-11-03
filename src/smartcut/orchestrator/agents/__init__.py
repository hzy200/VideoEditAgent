"""默认子智能体集合。"""

from __future__ import annotations

from smartcut.orchestrator.agents.analyze import AnalyzeAgent
from smartcut.orchestrator.agents.audio import AudioAgent
from smartcut.orchestrator.agents.candidates import CandidateAgent
from smartcut.orchestrator.agents.chapters import ChapterizerAgent
from smartcut.orchestrator.agents.compile import CompileAgent
from smartcut.orchestrator.agents.contactsheet import ContactSheetAgent
from smartcut.orchestrator.agents.critic import CriticAgent
from smartcut.orchestrator.agents.faces import FacesAgent
from smartcut.orchestrator.agents.frames import FramesAgent
from smartcut.orchestrator.agents.plan import PlanAgent
from smartcut.orchestrator.agents.qa import SemanticQAAgent
from smartcut.orchestrator.agents.quality import QualityAgent
from smartcut.orchestrator.agents.render import RenderAgent
from smartcut.orchestrator.agents.replan import ReplanAgent
from smartcut.orchestrator.agents.scenes import ScenesAgent
from smartcut.orchestrator.agents.transcribe import TranscribeAgent
from smartcut.orchestrator.agents.verify import VerifyAgent
from smartcut.orchestrator.base import SubAgent


def build_default_agents() -> dict[str, SubAgent]:
    agents: list[SubAgent] = [
        AnalyzeAgent(),
        FramesAgent(),
        ScenesAgent(),
        QualityAgent(),
        FacesAgent(),
        ContactSheetAgent(),
        ChapterizerAgent(),
        CandidateAgent(),
        TranscribeAgent(),
        PlanAgent(),
        AudioAgent(),
        CriticAgent(),
        SemanticQAAgent(),
        ReplanAgent(),
        CompileAgent(),
        RenderAgent(),
        VerifyAgent(),
    ]
    return {agent.name: agent for agent in agents}


__all__ = [
    "AnalyzeAgent",
    "AudioAgent",
    "CandidateAgent",
    "ChapterizerAgent",
    "CompileAgent",
    "ContactSheetAgent",
    "CriticAgent",
    "FacesAgent",
    "FramesAgent",
    "PlanAgent",
    "QualityAgent",
    "RenderAgent",
    "ReplanAgent",
    "ScenesAgent",
    "SemanticQAAgent",
    "TranscribeAgent",
    "VerifyAgent",
    "build_default_agents",
]
