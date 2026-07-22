# Project Plan: Local RAG over Sell-Side Research + "Crowd vs. Street" Analytics

**Course:** LLM assignment (MSBA)
**Author:** Hayden
**Date:** 2026-07-21
**Assignment option:** LLM technology deep dive (RAG implemented from scratch), extended with a comparative analytics layer.
**Target hardware:** Linux box, RTX 5070 (12GB VRAM), 32GB system RAM, 1TB+ free disk, fast CPU. Serving via Ollama. Full ~800K-doc / ~800GB corpus.

---

## 1. One-line thesis

Build a fully local RAG engine over a large sell-side research corpus, then use it to answer a question no single dataset can answer alone: **when the professionals (sell-side analysts) and the crowd (a retail stock chat) disagree about a stock, who is right?** Realized forward returns already sitting in the database serve as the scoreboard.

This satisfies the "from scratch" requirement (you build chunking, indexing, retrieval, reranking, and prompt assembly yourself, not a hosted RAG API) and produces a real, defensible finding instead of a toy demo.

---

## 2. Why this is a strong project

The from-scratch RAG build is the ML-engineering deliverable the assignment asks for. The crowd-vs-street analysis is the differentiator: most students will build a chatbot over a few PDFs. You have 222K real research documents, 889K chat messages tagged to tickers, and pre-computed abnormal returns. Very few people have all three joined. The combination is the story you show an employer.

Everything runs locally on your Mac. No token bill: local embeddings, local generation (Gemma via Ollama), and the analytics is SQL plus pandas.

---

## 3. Data inventory (verified)

**`research.db` -> `research_reports`** (851,762 rows, dates 1982-2026)
- Downloaded PDFs with text (not scanned): **222,704**, in `output/research_pdfs/2026/`.
- 640,235 rows carry a real ticker; metadata includes provider, date, region, sector, GICS, title.
- Top providers: JPMorgan (152K), Morgan Stanley (62K), BofA (52K), UBS (47K), Jefferies (42K), Barclays (30K), Deutsche Bank (24K), RBC (21K).

**`godel_rest.db`** (retail stock chat + market data)
- `chat_messages`: 889,113 messages, 5,760 users, Feb 2024 to Jul 2026.
- `chat_tickers`: 145,660 message-to-ticker tags across 4,807 tickers.
- `events`: 126,407 messages labeled with `direction` (-1/0/+1) and `sent_score` (labeled_by "hybrid"). Distribution: 24,711 bearish, 66,806 neutral, 34,890 bullish.
- `event_returns`: 119,161 rows (117,542 with `abn_5d`) holding `ret_1d/5d/20d` and abnormal returns `abn_1d/5d/20d`. **This is the ground-truth scoreboard.**
- `prices`: 1,088,248 OHLCV rows, Oct 2024 to Jun 2026.
- `news_items`: sparse (15 rows), ignore for now.

**Join window.** All four sources (research, chat, events/returns, prices) overlap from roughly **Oct 2024 to Jun 2026**. Scope the comparative analysis to that window. The RAG corpus itself can use everything downloaded.

**Character of the chat.** Ticker volume skews speculative and momentum-driven: RGTI, IONQ, QBTS, QUBT (quantum), SRPT, ABVX (biotech), NVDA, MU, INTC, TSLA, SPY, plus crypto (BTCUSD). Good news for the thesis: these are exactly the names where crowd and street are most likely to diverge.

---

## 4. System architecture

Two subsystems that meet at the answer layer.

**A. RAG engine (the from-scratch build)**
```
PDFs -> parse/clean -> chunk (+metadata) -> local embed -> vector index (FAISS)
                                          -> BM25 index
query -> hybrid retrieve (dense + sparse) -> cross-encoder rerank
      -> prompt assembly (grounded, cited) -> local LLM (Gemma/Ollama) -> answer + sources
```

**B. Crowd-vs-Street analytics**
```
research_reports (ticker,date,provider) ---\
                                            >-- join on ticker + date-window --> comparison table
chat events (ticker,ts,direction) --------/
                                            \-- event_returns (abn_5d) --> scoreboard
```

**C. Integration.** The chat UI answers two modes:
1. *Research Q&A:* "What is the sell-side view on IONQ this quarter?" -> RAG over PDFs, cited.
2. *Crowd-vs-street:* "Where do analysts and the chat disagree, and who's been right?" -> analytics layer, with RAG pulling the relevant report snippets as evidence.

---

## 5. From-scratch RAG design (what you implement yourself)

The graded "from scratch" content is the machinery around the models. Using PyTorch, sentence-transformers, FAISS, and a local LLM is explicitly allowed by the assignment; calling a hosted RAG endpoint is not.

- **Parsing/cleaning:** pdfplumber for text; strip the repeated FINRA/disclaimer boilerplate and headers/footers (they pollute retrieval). Keep page numbers for citation.
- **Chunking:** you write this. Recursive/structural chunking with overlap; attach metadata (source file, page, ticker, provider, date). Test 2-3 chunk sizes as an experiment.
- **Embeddings (local):** sentence-transformers with a local model (candidates: `bge-small-en-v1.5`, `bge-large-en-v1.5`, or `nomic-embed-text`). Batch on the Mac's GPU (MPS).
- **Vector index:** FAISS (flat for the subset, IVF/HNSW when you scale). You own the add/search code.
- **Sparse index:** BM25 (rank_bm25 or a small custom implementation) for hybrid retrieval.
- **Hybrid retrieval + fusion:** combine dense and sparse candidates (reciprocal rank fusion). You implement the fusion.
- **Reranking:** a cross-encoder (`bge-reranker-base`) over the fused candidates. Local.
- **Prompt assembly:** you build the grounding template that forces citations and refuses when context is thin.
- **Generation (local):** Gemma via Ollama. See section 7 on model choice.

Deliverable is a working `ask()` that returns an answer plus the source PDFs and pages it used.

---

## 6. Crowd-vs-Street analytics design

Reuse the labeling and return work already in `godel_rest.db`; do not rebuild it.

- **Street signal:** derive a per-ticker, per-week sell-side stance from `research_reports`. Minimal version: report volume and provider coverage over time. Stronger version: run the local LLM once over each report's first pages to extract a stance (bullish/neutral/bearish) and price-target direction, cached to a new table so it is a one-time cost.
- **Crowd signal:** aggregate `events.direction`/`sent_score` per ticker per week.
- **Scoreboard:** attribute realized `abn_5d`/`abn_20d` from `event_returns` to each side. Who was directionally right, and by how much, conditioned on agreement vs. disagreement?
- **Headline analyses:** (a) hit-rate of crowd vs. street on divergence weeks; (b) lead-lag, does chat sentiment move before or after research is published; (c) which sectors/tickers the crowd beats the street on (guess: speculative quantum/biotech names).

These are SQL + pandas, no LLM required except the optional stance extraction.

---

## 7. Local model plan (RTX 5070, 12GB VRAM)

The 12GB VRAM ceiling is the binding constraint; it caps the generation model. Everything else (embeddings, reranker) is small and runs with headroom. CUDA on the 5070 is faster than a Mac's MPS, and you can run ingest jobs overnight.

- **Generation (fits 12GB at Q4/Q5):** `gemma2:9b` or `gemma3:12b` as the default; strong quality, fast, leaves room for context. Also A/B `qwen2.5:14b-instruct` at Q4 (~9GB), which tends to be better at structured/financial reasoning. Serve via Ollama (simplest) or vLLM (higher batch throughput if you want it).
- **Do not target 27B+ for the interactive path:** at Q4 it needs ~16GB and only runs via slow CPU offload. Optionally use a 27B offline for one-time report-stance extraction where latency does not matter.
- **Embeddings:** local sentence-transformers (BGE-base/large or nomic-embed), <2GB on GPU. Run in a separate process from the LLM, or batch-embed the whole corpus first then load the LLM, so they do not contend for VRAM.
- **Reranker:** local BGE cross-encoder (`bge-reranker-base`), small.

---

## 8. Scale strategy (full ~800K corpus)

At 800K docs the hard part is data engineering, not the model. That is a plus for an ML-engineering portfolio. Rough scale: ~800K PDFs at ~12 pages average chunk into an estimated **25-40M chunks**. Two implications:

- **PDF parsing is the long pole.** It is CPU-bound and will likely take longer than embedding (plausibly a day-plus across the full corpus). Parallelize with multiprocessing and **cache parsed text to disk** so the corpus is never parsed twice. Log failures; some PDFs will be malformed.
- **Index must be compressed.** A flat FAISS index of ~30M x 768 float32 is ~90GB, too large to hold comfortably in RAM. Use **IVF-PQ** (or HNSW+PQ), which compresses to a few GB and still gives sub-second search. Implementing this index yourself is exactly the from-scratch engineering the assignment rewards.

**Approach:** build and evaluate the entire pipeline on a **stratified subset (~5K reports over the chat's top ~50 tickers, Oct 2024-Jun 2026)** so it lines up with the analytics window. Once the pipeline is proven, run the full ingest overnight. The demo can run on either; the report documents the full-scale run and its throughput numbers.

### 8a. Storage budget (verified estimates)

The PDFs are large because they are binary containers with embedded fonts, charts, and images (~1MB/doc average). The extracted text and its embeddings are far smaller. Estimates assume ~800K docs -> ~15-35M chunks (25M midpoint), 768-dim embeddings:

| Artifact | Estimated size | Notes |
|---|---|---|
| Original PDFs | ~800GB | On disk, read-only source |
| Extracted text cache | ~25-40GB | Parse once, reuse forever |
| Flat float32 index (768-dim) | ~75-95GB | Do NOT use: exceeds 32GB RAM |
| **IVF-PQ compressed index** | **~2-6GB** | **Recommended: fits in RAM, sub-second search** |
| Chunk metadata (sqlite/parquet) | ~5-15GB | ids, source, page, ticker, date |

**The binding resource is the 32GB RAM, not disk.** A flat index cannot be held in memory, so compress with IVF-PQ (or HNSW+PQ); the compressed index loads fully into RAM and searches 25M vectors in well under a second. With 1TB+ free, disk is a non-issue; keep the parsed-text cache and index on disk and load the index into RAM at query time.

**Optional size levers** (probably unnecessary given the disk headroom): use a 384-dim model (`bge-small`) to roughly halve the flat/index size, or apply Matryoshka embedding truncation.

---

## 9. Evaluation (the "balanced" requirement)

Numbers, not just a demo.

- **Retrieval eval:** hand-build or LLM-bootstrap a set of ~40-60 question -> correct-source pairs. Measure hit-rate@k and MRR. Compare at least two configs (e.g., dense-only vs. hybrid+rerank, and two chunk sizes).
- **Answer grounding:** spot-check that citations actually support the answer; report a faithfulness rate.
- **Analytics validity:** report the crowd-vs-street hit-rates with sample sizes and a naive baseline (e.g., always-bullish), so the finding is honest.

---

## 10. Milestones

1. **Data prep:** build the stratified subset, parse + clean PDFs, verify joins. (Foundational.)
2. **Index:** chunk, embed, build FAISS + BM25.
3. **Retrieve + rerank:** hybrid retrieval, fusion, cross-encoder; sanity-check on sample queries.
4. **Generate:** wire Gemma via Ollama, grounded/cited prompt, refusal behavior.
5. **UI:** minimal Streamlit chat with the two modes.
6. **Analytics:** crowd-vs-street joins, scoreboard, 2-3 headline charts.
7. **Eval:** retrieval metrics + one honest finding.
8. **Report + demo:** architecture, tradeoffs, results, limitations.

---

## 11. Deliverables

- Working local system (RAG engine + Streamlit UI + analytics module).
- Evaluation results (retrieval metrics table, one crowd-vs-street finding with charts).
- Written report (.docx for the final hand-in) documenting design, from-scratch components, tradeoffs, and limitations.

---

## 12. Risks and open items

- **Licensing / permitted use:** confirm you are allowed to build on this research data for a school project. Flagging, not blocking.
- **Boilerplate contamination:** the disclaimer text repeats across every PDF and will hurt retrieval if not stripped. Budget time for cleaning.
- **Sentiment label trust:** `events.direction` is "hybrid"-labeled; audit a sample before leaning on it for the scoreboard.
- **RAM ceiling drives the index choice:** 32GB system RAM means the index must be PQ-compressed (see 8a). Resolved by using IVF-PQ.
- **Crypto/odd tickers:** `chat_tickers` includes crypto and meme symbols with no research coverage; exclude from the comparison set.

---

## Confirmed environment

- RTX 5070, 12GB VRAM -> generation model in the 9-14B class at Q4.
- 32GB system RAM -> IVF-PQ compressed index (fits in RAM).
- 1TB+ free disk -> no storage constraint; keep PDFs, text cache, and index on disk.
- Serving via Ollama.

## Open questions for you

1. OK to create a `rag_project/` folder and a cached `report_stance` table alongside the data, or keep everything isolated?
2. For the scoreboard, prefer `abn_5d` (5-day abnormal return) as the primary metric, or a different horizon?
