"""Assistant opérateur (RAG local) — J2. Ollama + LangChain + Chroma sur les documents de docs/.
Lecture seule : aucun accès Docker, aucun secret, aucun droit sur la plateforme (sujet §3.3).
Validé le 19/10 (Vitor) contre un Ollama distant : qwen3:8b (8,2 Md — exception accordée par les coachs) + embeddinggemma.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "embeddinggemma")
DOCS_PATH = Path(os.environ.get("DOCS_PATH", "/docs"))

PROMPT = ChatPromptTemplate.from_messages([
    ("system", "Tu es l'assistant des opérateurs de la plateforme RESILIENCE-ALPHA. Réponds en français, brièvement, "
               "uniquement à partir du contexte fourni (procédures, architecture, gouvernance). Si le contexte ne suffit pas, dis-le. "
               "Tu n'exécutes aucune action : tu expliques et tu renvoies vers la procédure."),
    ("human", "Contexte :\n{context}\n\nQuestion : {question}"),
])
state: dict = {}

# Prefixes de tache attendus par le modele d'embedding. Ollama transmet le texte
# tel quel et LangChain ne les ajoute pas : sans eux, la marge moyenne entre le
# bon chunk et le suivant passe de +0,238 a +0,205 (mesure sur embeddinggemma).
# Un modele absent de cette table garde le comportement actuel, sans prefixe.
EMBED_PREFIXES = {
    "embeddinggemma": ("title: none | text: ", "task: search result | query: "),
}


class PrefixedOllamaEmbeddings(OllamaEmbeddings):
    """OllamaEmbeddings appliquant les prefixes de tache attendus par le modele.

    Asymetrique a dessein : LangChain appelle embed_documents() a l'indexation
    et embed_query() a la recherche, ce qui est exactement la separation dont
    ces prefixes ont besoin.
    """

    def _prefixes(self) -> tuple[str, str]:
        return EMBED_PREFIXES.get(self.model.split(":")[0], ("", ""))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        doc_prefix, _ = self._prefixes()
        return super().embed_documents([doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        _, query_prefix = self._prefixes()
        return super().embed_query(query_prefix + text)


def build_index():
    docs = []
    for p in sorted(DOCS_PATH.rglob("*.md")):
        docs.append(Document(page_content=p.read_text(encoding="utf-8"), metadata={"source": str(p.relative_to(DOCS_PATH))}))
    chunks = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=120).split_documents(docs)
    embeddings = PrefixedOllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)
    # Index en mémoire, reconstruit à chaque démarrage (~5 s) : pas de persistance = pas de documents dupliqués
    # ni de vecteurs de deux modèles d'embedding mélangés si EMBED_MODEL change.
    state["store"] = Chroma.from_documents(chunks, embeddings, collection_name=f"docs-{EMBED_MODEL.replace(':', '-')}")
    # reasoning=False : qwen3 raisonne par defaut et produit ~550 caracteres
    # jetes ensuite (.content ne les contient pas). Mesure : 6,0 s -> 0,8 s par
    # reponse. Requiert langchain-ollama >= 0.3.4 et un modele qui sait raisonner.
    state["llm"] = ChatOllama(model=LLM_MODEL, base_url=OLLAMA_URL, temperature=0.1, reasoning=False)
    state["n_docs"], state["n_chunks"] = len(docs), len(chunks)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        build_index()
    except Exception as exc:  # noqa: BLE001 — l'API reste debout pour afficher l'erreur
        state["error"] = repr(exc)
    yield


app = FastAPI(title="RESILIENCE-ALPHA — assistant RAG", lifespan=lifespan)


class Ask(BaseModel):
    # Bornes : la question arrive désormais du dashboard, visible sur le hotspot
    question: str = Field(min_length=3, max_length=500)
    k: int = Field(4, ge=1, le=8)


@app.post("/ask")
def ask(body: Ask):
    if "store" not in state:
        return {"error": state.get("error", "index non construit")}
    hits = state["store"].similarity_search(body.question, k=body.k)
    context = "\n\n---\n\n".join(f"[{h.metadata['source']}]\n{h.page_content}" for h in hits)
    answer = (PROMPT | state["llm"]).invoke({"context": context, "question": body.question}).content
    return {"answer": answer, "sources": sorted({h.metadata["source"] for h in hits})}


@app.post("/reindex")
def reindex():
    build_index()
    return {"docs": state["n_docs"], "chunks": state["n_chunks"]}


@app.get("/health")
def health():
    return {"status": "ok" if "store" in state else "degraded", "model": LLM_MODEL, "error": state.get("error")}
