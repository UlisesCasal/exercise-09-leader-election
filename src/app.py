import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from src.database import Base, engine, get_db
from src.models import Node
from src.schemas import NodeCreate, NodeResponse, NodeUpdate
from src import election

try:
    Base.metadata.create_all(bind=engine)
except IntegrityError:
    pass  # Concurrent startup — another node already created the schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=election.heartbeat_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


# ── Election message schemas ──────────────────────────────────────────────────

class ElectionMessage(BaseModel):
    sender_id: int


class CoordinatorAnnouncement(BaseModel):
    leader_id: int


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    count = db.query(Node).filter(Node.status == "active").count()
    return {"status": "ok", "db": db_status, "nodes_count": count}


# ── Node registry ─────────────────────────────────────────────────────────────

@app.post("/api/nodes", response_model=NodeResponse, status_code=201)
def register_node(node: NodeCreate, db: Session = Depends(get_db)):
    existing = db.query(Node).filter(Node.name == node.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Node already exists")
    db_node = Node(name=node.name, host=node.host, port=node.port)
    db.add(db_node)
    db.commit()
    db.refresh(db_node)
    return db_node


@app.get("/api/nodes", response_model=list[NodeResponse])
def list_nodes(db: Session = Depends(get_db)):
    return db.query(Node).all()


@app.get("/api/nodes/{name}", response_model=NodeResponse)
def get_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


@app.put("/api/nodes/{name}", response_model=NodeResponse)
def update_node(name: str, update: NodeUpdate, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    if update.host is not None:
        node.host = update.host
    if update.port is not None:
        node.port = update.port
    node.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(node)
    return node


@app.delete("/api/nodes/{name}", status_code=204)
def delete_node(name: str, db: Session = Depends(get_db)):
    node = db.query(Node).filter(Node.name == name).first()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    node.status = "inactive"
    node.updated_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)


# ── Election ──────────────────────────────────────────────────────────────────

@app.get("/api/node-id")
def get_node_id():
    return {"node_id": election.NODE_ID}


@app.post("/api/election")
def trigger_election():
    threading.Thread(target=election.start_election, daemon=True).start()
    return {"status": "election started"}


@app.post("/api/election/message")
def receive_election_message(body: ElectionMessage):
    """Receives an ELECTION message from a lower-ID node.
    Returning 200 is the implicit OK — the receiver is alive and has a higher ID."""
    election.handle_election_message(body.sender_id)
    return {"status": "ok"}


@app.post("/api/coordinator")
def receive_coordinator(body: CoordinatorAnnouncement):
    election.set_leader(body.leader_id)
    return {"status": "ok"}


@app.get("/api/leader")
def get_leader():
    leader = election.get_leader()
    if leader is None:
        raise HTTPException(status_code=503, detail="No leader elected yet")
    return {"leader_id": leader}
