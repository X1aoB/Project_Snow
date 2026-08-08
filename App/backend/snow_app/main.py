"""FastAPI inspection service. Conversation generation remains stage-locked."""

from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings
from .contracts import (
    ConversationIdentity,
    EntityNodeReviewDecision,
    GraphNeighborhood,
    MVPChatRequest,
    MVPFeedbackRequest,
    MVPFeedbackIssueStatusRequest,
    MVPFeedbackTriageRequest,
    RelationReviewDecision,
    RetrievalRequest,
    RetrievalResponse,
    StageLockResponse,
)
from .repository import MACHINE_REVIEW_FILTERS, REVIEW_RISK_LEVELS, REVIEW_TIERS, RuntimeRepository
from .mvp_service import (
    MVPChatDisabled,
    MVPCommunicationConflict,
    MVPProviderError,
    MVPRequestInProgress,
    MVPService,
)


settings = Settings.from_environment()
repository = RuntimeRepository(settings)
mvp_service = MVPService(settings, repository)
app = FastAPI(title="Project Snow Application API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


def get_repository() -> RuntimeRepository:
    return repository


def _validate_review_filters(
    review_status: str,
    tier: str | None = None,
    risk_level: str | None = None,
    machine_verdict: str | None = None,
) -> None:
    if review_status not in {"pending_review", "approved", "rejected"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported review status.")
    if tier and tier not in REVIEW_TIERS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported review tier.")
    if risk_level and risk_level not in REVIEW_RISK_LEVELS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported review risk level.")
    if machine_verdict and machine_verdict not in MACHINE_REVIEW_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Unsupported machine review verdict filter.",
        )


def _validate_entity_review_status(review_status: str) -> None:
    if review_status not in {"pending_review", "approved", "rejected"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unsupported entity review status.")


@app.get("/health")
def health(repo: RuntimeRepository = Depends(get_repository)) -> dict:
    return {"service": "project-snow-api", "chat_enabled": settings.chat_enabled, "artifacts": repo.status()}


@app.get("/api/v1/characters")
def characters(repo: RuntimeRepository = Depends(get_repository)) -> list[dict]:
    return repo.list_characters()


@app.get("/api/v1/personas/{character_id}")
def persona(character_id: str, repo: RuntimeRepository = Depends(get_repository)) -> dict:
    profile = repo.get_persona(character_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Character persona evidence was not found.")
    return profile


@app.post("/api/v1/retrieval/preview", response_model=RetrievalResponse)
def retrieval_preview(request: RetrievalRequest, repo: RuntimeRepository = Depends(get_repository)) -> RetrievalResponse:
    fusion, vector_available, results = repo.hybrid_search(
        request.query,
        request.character_id,
        request.limit,
    )
    return RetrievalResponse(
        query=request.query,
        mode=request.mode,
        character_id=request.character_id,
        conversation_identity=ConversationIdentity(),
        fusion=fusion,
        vector_available=vector_available,
        results=results,
    )


@app.get("/api/v1/mvp/status")
def mvp_status() -> dict:
    return mvp_service.status()


@app.get("/api/v1/mvp/bootstrap")
def mvp_bootstrap() -> dict:
    return mvp_service.bootstrap()


@app.get("/api/v1/mvp/tools")
def mvp_tools() -> dict:
    """Expose the assistant's read-only capability contract to clients."""

    return {
        "mode": "assistant",
        "tools": mvp_service._assistant_tool_definitions(),
        "policy": {
            "read_only": True,
            "intent_gated": True,
            "automatic_time_sensitive_lookup": True,
            "public_web_only": True,
            "hidden_reasoning": "never_returned",
        },
    }


@app.get("/api/v1/mvp/questions")
def mvp_questions(character_id: str) -> dict:
    try:
        return mvp_service.questions(character_id)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MVP 角色视图不存在。") from None


@app.post("/api/v1/mvp/chat")
def mvp_chat(request: MVPChatRequest) -> dict:
    try:
        return mvp_service.chat(
            request.character_id,
            request.message,
            request.session_id,
            request.limit,
            request.costume_context,
            request.mode,
            request.world_session_id,
            request.communication_channel,
            request.presence_action,
            request.client_message_id,
            [item.model_dump() for item in request.analyst_content_blocks],
        )
    except MVPCommunicationConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
    except MVPRequestInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "request_in_progress", "message": str(exc)},
        ) from exc
    except MVPChatDisabled as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MVP 角色视图不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except MVPProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@app.post("/api/v1/mvp/feedback")
def mvp_feedback(request: MVPFeedbackRequest) -> dict:
    try:
        return mvp_service.append_feedback(
            request.character_id,
            request.session_id,
            request.message_id,
            request.selected_options,
            request.free_text,
            category=request.category,
            mode=request.mode,
            communication_channel=request.communication_channel,
            registry_version=request.registry_version,
            client_version=request.client_version,
            message_excerpt=request.message_excerpt,
            answer_excerpt=request.answer_excerpt,
        )
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MVP 角色视图不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@app.get("/api/v1/mvp/feedback")
def mvp_feedback_list(
    limit: int = 50,
    session_id: str | None = None,
    category: str | None = None,
    character_id: str | None = None,
    feedback_status: str | None = None,
    resolution_status: str | None = None,
) -> dict:
    return mvp_service.feedback(
        min(max(limit, 1), 200),
        session_id,
        category=category,
        character_id=character_id,
        feedback_status=feedback_status,
        resolution_status=resolution_status,
    )


@app.post("/api/v1/mvp/feedback/{feedback_id}/triage")
def mvp_feedback_triage(feedback_id: str, request: MVPFeedbackTriageRequest) -> dict:
    try:
        return mvp_service.triage_feedback(feedback_id, request.status, request.note)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="反馈不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@app.post("/api/v1/mvp/feedback/issues/{issue_key}/status")
def mvp_feedback_issue_status(issue_key: str, request: MVPFeedbackIssueStatusRequest) -> dict:
    try:
        return mvp_service.set_feedback_issue_status(issue_key, request.status, request.note)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@app.get("/api/v1/mvp/conversations/{character_id}")
def mvp_conversation_history(
    character_id: str,
    session_id: str | None = None,
    before: int | None = None,
    limit: int = 50,
) -> dict:
    try:
        return mvp_service.conversation_history(
            character_id,
            session_id=session_id,
            before=before,
            limit=min(max(limit, 1), 100),
        )
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MVP 角色视图不存在。") from None


@app.delete("/api/v1/mvp/conversations/{character_id}")
def mvp_clear_conversation(character_id: str, mode: str | None = None) -> dict:
    try:
        return mvp_service.clear_conversation(character_id, mode)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MVP 角色视图不存在。") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@app.get("/api/v1/graph/neighborhood/{graph_node_id}", response_model=GraphNeighborhood)
def graph_neighborhood(graph_node_id: str, repo: RuntimeRepository = Depends(get_repository)) -> GraphNeighborhood:
    result = repo.neighborhood(graph_node_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Graph node was not found.")
    return GraphNeighborhood(**result)


@app.get("/api/v1/review/relations/summary")
def relation_review_summary(repo: RuntimeRepository = Depends(get_repository)) -> dict:
    return repo.relation_review_summary()


@app.get("/api/v1/review/entities/summary")
def entity_review_summary(repo: RuntimeRepository = Depends(get_repository)) -> dict:
    return repo.entity_review_summary()


@app.get("/api/v1/review/entities/candidates")
def entity_review_candidates(
    review_status: str = "pending_review",
    limit: int = 20,
    offset: int = 0,
    repo: RuntimeRepository = Depends(get_repository),
) -> dict:
    _validate_entity_review_status(review_status)
    return repo.entity_review_candidates(review_status, min(max(limit, 1), 50), max(offset, 0))


@app.post("/api/v1/review/entities/candidates/{entity_candidate_id}/decision")
def decide_entity_node_candidate(
    entity_candidate_id: str,
    decision: EntityNodeReviewDecision,
    repo: RuntimeRepository = Depends(get_repository),
) -> dict:
    try:
        return repo.decide_entity_node_candidate(
            entity_candidate_id,
            decision.decision,
            decision.reviewer_id,
            decision.note,
        )
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity node candidate was not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@app.get("/api/v1/review/relations/candidates")
def relation_review_candidates(
    review_status: str = "pending_review", limit: int = 20, repo: RuntimeRepository = Depends(get_repository)
) -> list[dict]:
    _validate_review_filters(review_status)
    return repo.relation_review_candidates(review_status, min(max(limit, 1), 50))


@app.get("/api/v1/review/relations/triage")
def relation_review_triage(
    review_status: str = "pending_review", repo: RuntimeRepository = Depends(get_repository)
) -> dict:
    _validate_review_filters(review_status)
    return repo.relation_review_triage_summary(review_status)


@app.get("/api/v1/review/relations/groups")
def relation_review_groups(
    review_status: str = "pending_review",
    limit: int = 12,
    offset: int = 0,
    tier: str | None = None,
    relation_type: str | None = None,
    source_type: str | None = None,
    risk_level: str | None = None,
    machine_verdict: str | None = None,
    repo: RuntimeRepository = Depends(get_repository),
) -> dict:
    _validate_review_filters(review_status, tier, risk_level, machine_verdict)
    return repo.relation_review_groups(
        review_status=review_status,
        limit=min(max(limit, 1), 50),
        offset=max(offset, 0),
        tier=tier,
        relation_type=relation_type.strip().upper() if relation_type else None,
        source_type=source_type,
        risk_level=risk_level,
        machine_verdict=machine_verdict,
    )


@app.get("/api/v1/review/relations/groups/{review_group_id}")
def relation_review_group_detail(
    review_group_id: str,
    review_status: str = "pending_review",
    candidate_limit: int = 12,
    candidate_offset: int = 0,
    repo: RuntimeRepository = Depends(get_repository),
) -> dict:
    _validate_review_filters(review_status)
    detail = repo.relation_review_group_detail(
        review_group_id,
        review_status,
        candidate_limit=min(max(candidate_limit, 1), 30),
        candidate_offset=max(candidate_offset, 0),
    )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relation review group was not found.")
    return detail


@app.get("/api/v1/review/relations/audit-sample")
def relation_review_audit_sample(
    review_status: str = "pending_review",
    size: int = 12,
    seed: str = "project-snow-audit-v1",
    tier: str | None = None,
    relation_type: str | None = None,
    source_type: str | None = None,
    risk_level: str | None = None,
    machine_verdict: str | None = None,
    repo: RuntimeRepository = Depends(get_repository),
) -> dict:
    _validate_review_filters(review_status, tier, risk_level, machine_verdict)
    return repo.relation_review_audit_sample(
        review_status=review_status,
        size=min(max(size, 1), 50),
        seed=seed[:160] or "project-snow-audit-v1",
        tier=tier,
        relation_type=relation_type.strip().upper() if relation_type else None,
        source_type=source_type,
        risk_level=risk_level,
        machine_verdict=machine_verdict,
    )


@app.post("/api/v1/review/relations/candidates/{candidate_id}/decision")
def decide_relation_candidate(
    candidate_id: str, decision: RelationReviewDecision, repo: RuntimeRepository = Depends(get_repository)
) -> dict:
    try:
        return repo.decide_relation_candidate(
            candidate_id,
            decision.decision,
            decision.reviewer_id,
            decision.note,
            decision.from_node_id,
            decision.to_node_id,
        )
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relation candidate was not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@app.post("/api/v1/admin/reload-artifacts", status_code=status.HTTP_204_NO_CONTENT)
def reload_artifacts(repo: RuntimeRepository = Depends(get_repository)) -> Response:
    repo.clear_caches()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/api/v1/chat", response_model=StageLockResponse, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
def chat_stage_lock() -> StageLockResponse:
    if settings.chat_enabled:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Conversation generator is intentionally not implemented in this architecture phase.",
        )
    return StageLockResponse(
        code="conversation_stage_locked",
        message="B and C are being validated. Conversation generation is not exposed as a product endpoint.",
        required_stages=["B persona-first hybrid retrieval", "C reviewed knowledge graph"],
        conversation_identity=ConversationIdentity(),
    )


if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run("backend.snow_app.main:app", host=os.getenv("API_HOST", "0.0.0.0"), port=int(os.getenv("API_PORT", "8000")))
