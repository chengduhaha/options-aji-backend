# Baidu Netdisk Member Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import Baidu Netdisk PDF archives into the existing blog attachment library and expose non-sample documents only to signed-in members/admins.

**Architecture:** Reuse the current `blog_attachments` table and local PDF storage. Add a focused import module plus CLI script for Baidu directory scans/downloads, and extend blog document/file endpoints to understand public samples versus member-only documents.

**Tech Stack:** FastAPI, SQLAlchemy, pytest, requests, Next.js client fetch helpers.

---

### File Structure

- Create: `app/services/baidu_netdisk_import.py` for category inference, title/description generation, dedupe, listing, and download/import helpers.
- Create: `scripts/import_baidu_blog_documents.py` as the operator CLI for dry-run and real import.
- Modify: `app/api/routes/blog.py` to allow members/admins to list and download member-only standalone PDFs.
- Modify: `tests/test_blog.py` with member document access tests.
- Modify frontend `lib/blog/types.ts`, `lib/blog/api.ts`, and `components/blog/BlogDocumentsPageClient.tsx` so the member library page sends JWT and renders access metadata.

### Task 1: Backend Member Access

- [ ] Add failing tests for anonymous, member, and admin access to sample/non-sample standalone documents.
- [ ] Run `pytest tests/test_blog.py -q` and confirm the new tests fail because non-sample standalone PDFs are hidden.
- [ ] Extend blog document listing and attachment download authorization to use `get_v3_access`.
- [ ] Run `pytest tests/test_blog.py -q` and confirm all blog tests pass.

### Task 2: Baidu Import Service

- [ ] Add failing unit tests for category mapping, duplicate detection, and dry-run summaries.
- [ ] Run the focused test and confirm it fails because the service module does not exist.
- [ ] Implement `baidu_netdisk_import.py` with no token hardcoding, `BAIDU_NETDISK_ACCESS_TOKEN` lookup, disk-space guard, rate-limited list calls, dlink download support, and existing `store_pdf`.
- [ ] Run the focused tests and blog tests.

### Task 3: Import CLI

- [ ] Add a CLI smoke test around argument parsing/dry-run behavior if practical without network.
- [ ] Implement `scripts/import_baidu_blog_documents.py` with `--root`, `--dry-run`, `--limit`, `--min-free-gb`, and `--yes`.
- [ ] Validate syntax with `python -m compileall app/services/baidu_netdisk_import.py scripts/import_baidu_blog_documents.py`.

### Task 4: Frontend Member Library UX

- [ ] Add/update frontend tests asserting document fetch uses auth and access metadata exists.
- [ ] Update types/API client and the documents page to call the member-aware endpoint with JWT automatically.
- [ ] Run `node --test tests/*.test.mjs` and `npm run build`.

### Task 5: Verification and Deployment

- [ ] Run backend tests for blog/import code.
- [ ] Run frontend tests/build.
- [ ] Commit backend and frontend changes separately.
- [ ] Run dry-run against `/基础专属会员资料库` and `/高级专属会员资料库`.
- [ ] Only after dry-run looks correct, run real import locally with disk guard.
- [ ] Deploy/restart backend if needed and deploy frontend through git/Vercel.
