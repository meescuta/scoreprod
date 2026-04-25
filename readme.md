# scoreprod — API de Score de Calote

API que calcula a probabilidade de calote de um lead da Me Escuta com base no histórico de mensagens com a Clara.

## Como funciona

Quando a Clara finaliza a qualificação, o N8N dispara um POST com o `lead_id`. A API busca o histórico de mensagens no Postgres, extrai 3 features, roda o modelo e posta o score como nota no Kommo.

```
N8N → POST /score { lead_id }
        ↓
Busca msgs no Postgres (n8n_chat_histories)
        ↓
Extrai features → roda scorecard WoE
        ↓
Posta nota no Kommo
```

## Features

| Feature | Fórmula |
|---------|---------|
| `razao_msgs_cliente_atendente` | msgs do cliente ÷ msgs da Clara |
| `taxa_mencoes_terceiro_por_100msgs` | menções a terceiro × 100 ÷ total de msgs |
| `flag_mencao_terceiro_alguma_vez` | 1 se mencionou terceiro, 0 se não |

## Score e Tiers

| Tier | Score | Ação |
|------|-------|------|
| A | 0–25 | Parcelamento normal |
| B | 25–50 | Atenção |
| C | 50–75 | Preferir Pix, parcelamento curto |
| D | 75–100 | Pix à vista, avaliar recusa |

## Endpoints

```
GET  /health  → { "status": "ok" }
POST /score   → { "lead_id": "123" }
```

## Variáveis de Ambiente

```
DATABASE_URL=postgresql://...
KOMMO_TOKEN=...
KOMMO_SUBDOMAIN=meescutakommo
SCORECARD_PATH=scorecard_20260420.json
```

## Rodar local

```bash
pip install -r requirements.txt
cp .env.example .env  # preencher vars
python -m uvicorn main:app --host 0.0.0.0 --port 8001
```

## Deploy

Hospedado no EasyPanel via Docker. Push na `main` → reimplantar manualmente.