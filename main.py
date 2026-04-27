import re
import json
import math
import os
import httpx
from datetime import datetime
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# --- config ---
DATABASE_URL   = os.environ["DATABASE_URL"]
KOMMO_TOKEN    = os.environ["KOMMO_TOKEN"]
KOMMO_SUB      = os.environ.get("KOMMO_SUBDOMAIN", "meescutakommo")
SCORECARD_PATH = os.environ.get("SCORECARD_PATH", "scorecard_20260420.json")

db = create_engine(DATABASE_URL)

with open(SCORECARD_PATH) as f:
    SCORECARD = json.load(f)

# regex de menção a terceiro
TERCEIRO_RE = re.compile(
    r"\b(minha\s+mãe|meu\s+pai|minha\s+avó|meu\s+avô|minha\s+esposa|meu\s+esposo"
    r"|minha\s+filha|meu\s+filho|minha\s+irmã|meu\s+irmão|minha\s+tia|meu\s+tio"
    r"|minha\s+sogra|meu\s+sogro|pra\s+(?:ele|ela|eles|elas)"
    r"|é\s+pra\s+(?:minha|meu|a|o)\s+(?:mãe|pai|avó|avô|esposa|esposo|filha|filho|irmã|irmão|tia|tio|sogra|sogro)"
    r"|é\s+pro\s+(?:meu|pai|avô|filho|irmão|marido|tio|sogro)"
    r"|do\s+meu\s+(?:pai|marido|filho|irmão|avô|tio|sogro|esposo)"
    r"|da\s+minha\s+(?:mãe|esposa|filha|irmã|avó|tia|sogra)"
    r"|(?:vó|vô)\s+(?:dele|dela)"
    r"|marido|esposo|esposa|neta|neto|filha|filho|sobrinha|sobrinho"
    r"|mãe\s+dele|pai\s+dele|mãe\s+dela|pai\s+dela)\b",
    re.IGNORECASE,
)

# --- schemas ---
class HandoffPayload(BaseModel):
    lead_id: Optional[str] = None             # handoff da Clara (chat)
    lead_id_consulta: Optional[str] = None    # consulta realizada (notas)

    @property
    def id(self) -> str:
        lead = self.lead_id or self.lead_id_consulta
        if not lead:
            raise HTTPException(status_code=422, detail="lead_id ou lead_id_consulta obrigatorio")
        return lead

    @property
    def origem(self) -> str:
        return "consulta" if self.lead_id_consulta else "clara"

# --- helpers comuns ---
def _woe_transform(feature_name: str, value: float) -> float:
    bins_info = SCORECARD["bins_woe"][feature_name]
    bins = bins_info["bins"]
    woes = bins_info["woe"]

    for i, bin_str in enumerate(bins):
        if bin_str in ("Special", "Missing"):
            continue
        s = bin_str.strip("([)]")
        parts = s.split(",")
        lo_str, hi_str = parts[0].strip(), parts[1].strip()
        lo = float("-inf") if lo_str == "-inf" else float(lo_str)
        hi = float("inf")  if hi_str == "inf"  else float(hi_str)

        lo_inc = bin_str.startswith("[")
        hi_inc = bin_str.endswith("]")

        in_lo = (value >= lo) if lo_inc else (value > lo)
        in_hi = (value <= hi) if hi_inc else (value < hi)

        if in_lo and in_hi:
            return woes[i]

    return 0.0

def calcular_score(features: dict) -> tuple[int, str]:
    coefs     = SCORECARD["coeficientes_lr"]
    intercept = SCORECARD["intercept"]

    logit = intercept
    for feat, coef in coefs.items():
        woe = _woe_transform(feat, features[feat])
        logit += coef * woe

    prob  = 1 / (1 + math.exp(-logit))
    score = int(prob * 100)
    tier  = "A" if score < 25 else "B" if score < 50 else "C" if score < 75 else "D"
    return score, tier

async def postar_nota_kommo(lead_id: str, score: int, tier: str, origem: str):
    texto = (
        f"Score de calote: {score} (tier {tier})\n"
        f"Origem: {origem} | Modelo: v20260420 | Calculado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    url = f"https://{KOMMO_SUB}.kommo.com/api/v4/leads/notes"
    payload = [{
        "entity_id": int(lead_id),
        "note_type": "common",
        "params": {"text": texto}
    }]

    async with httpx.AsyncClient() as client:
        r = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {KOMMO_TOKEN}"},
        )
        print(f"Kommo response: {r.status_code} - {r.text}")
        r.raise_for_status()

# --- modelo 1: features do chat (Postgres) ---
def fetch_messages(lead_id: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT message
                FROM n8n_chat_histories
                WHERE session_id = :sid
                ORDER BY id ASC
            """),
            {"sid": lead_id},
        ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail=f"Nenhuma mensagem para lead_id={lead_id}")

    return [r[0] for r in rows]

def extract_features_chat(msgs: list[dict]) -> dict:
    n_cliente   = sum(1 for m in msgs if m.get("type") == "human")
    n_atendente = sum(1 for m in msgs if m.get("type") == "ai")

    razao = n_cliente / n_atendente if n_atendente else 0.0

    n_terceiro = sum(
        len(TERCEIRO_RE.findall(m.get("content", "")))
        for m in msgs if m.get("type") == "human"
    )
    flag_terceiro = n_terceiro > 0
    taxa_terceiro = (n_terceiro * 100 / len(msgs)) if msgs else 0.0

    return {
        "razao_msgs_cliente_atendente":      razao,
        "taxa_mencoes_terceiro_por_100msgs":  taxa_terceiro,
        "flag_mencao_terceiro_alguma_vez":    float(flag_terceiro),
    }

# --- modelo 2: features das notas (Kommo API) ---
async def fetch_notas_kommo(lead_id: str) -> list[str]:
    url = f"https://{KOMMO_SUB}.kommo.com/api/v4/leads/{lead_id}/notes?limit=250"

    async with httpx.AsyncClient() as client:
        r = await client.get(
            url,
            headers={"Authorization": f"Bearer {KOMMO_TOKEN}"},
        )
        r.raise_for_status()
        data = r.json()

    notas = data.get("_embedded", {}).get("notes", [])

    # filtra só notas humanas (created_by != 0 = criadas por usuário, não sistema)
    textos = [
        n["params"]["text"]
        for n in notas
        if n.get("note_type") == "common"
        and n.get("created_by", 0) != 0
        and n.get("params", {}).get("text")
    ]

    if not textos:
        raise HTTPException(status_code=404, detail=f"Nenhuma nota humana para lead_id={lead_id}")

    return textos

def extract_features_notas(textos: list[str]) -> dict:
    texto_completo = " ".join(textos)
    total_palavras = len(texto_completo.split())

    n_terceiro = len(TERCEIRO_RE.findall(texto_completo))
    flag_terceiro = n_terceiro > 0

    # normaliza por 100 palavras (equivalente ao por 100 msgs)
    taxa_terceiro = (n_terceiro * 100 / total_palavras) if total_palavras else 0.0

    # razao não faz sentido em notas (sempre fono) — fixa em 1.0 (neutro)
    razao = 1.0

    return {
        "razao_msgs_cliente_atendente":      razao,
        "taxa_mencoes_terceiro_por_100msgs":  taxa_terceiro,
        "flag_mencao_terceiro_alguma_vez":    float(flag_terceiro),
    }

# --- endpoints ---
@app.post("/score")
async def score(payload: HandoffPayload):
    lead_id  = payload.id
    origem   = payload.origem

    if origem == "clara":
        msgs     = fetch_messages(lead_id)
        features = extract_features_chat(msgs)
    else:
        textos   = await fetch_notas_kommo(lead_id)
        features = extract_features_notas(textos)

    score, tier = calcular_score(features)
    await postar_nota_kommo(lead_id, score, tier, origem)

    return {"lead_id": lead_id, "origem": origem, "score": score, "tier": tier, "features": features}

@app.get("/health")
def health():
    return {"status": "ok"}