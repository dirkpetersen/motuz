# Document viewer (PDF, DOCX, XLSX, PPTX): status and remaining work

Paused on 2026-09-27. The work so far is on branch `wip-document-viewer` (a8829cd, pushed to
github.com/dirkpetersen/motuz). It contains the image and Markdown viewer branch
(`worktree-agent-a0b0c3d823673d7cb`) merged in, so merge that one into `dirk` first.

## Decisions (from the user)

- Everything renders in the browser. **No LibreOffice and no server-side conversion.**
- Files never leave Motuz: no Office Online, Google Docs viewer or other external service.
- PDF with pdf.js, not PyMuPDF/pymupdf4llm (AGPL, runs a C parser on the server, renders
  images instead of selectable text, needs the whole file).

## What exists on the branch

- Backend: `POST /api/system/files/view/document/` (`utils/document_view.py`,
  `system_manager.py`, `system_views.py`, local and rclone readers): type from magic bytes
  (`%PDF-`, zip), size cap `MOTUZ_VIEW_DOCUMENT_MAX_BYTES` (default 50 MiB), byte ranges for
  pdf.js, headers like the image endpoint (nosniff, `default-src 'none'; sandbox`, no-store).
  Unit tests: `test/backend/test_document_view.py`.
- Libraries (exact versions in `package.json`): `pdfjs-dist` 6.3.289 (worker bundled by
  webpack, no CDN), `docx-preview` 0.4.1, `dompurify` 3.4.16, `fflate` 0.8.3, SheetJS
  `xlsx` 0.20.3 from cdn.sheetjs.com (the npm registry copy is stale, CVE-2023-30533).
- Frontend: `views/Dialogs/DocumentViewer/` (`DocumentViewerDialog`, `PdfView`, `DocxView`,
  `SheetView`, `PptxView`), parsing in a Web Worker (`workers/documentWorker.js`,
  `documentWorkerClient.js`), helpers with node tests (`utils/zipSafe.js` zip-bomb limits from
  the central directory, `officeFiles.js`, `xmlLite.js`, `sheetGrid.js`, `pdfRanges.js`,
  `cssSafe.js`, `viewerKind.js`; `test/frontend/test_document_viewer.mjs`).
- PPTX: own outline view (slide titles, text, embedded png/jpeg/gif/webp images), no
  third-party PPTX renderer.
- e2e: fixtures generator `test/e2e/document_fixtures.py`, API checks in `e2e_test.py`, UI
  flows in `ui/ui_test.mjs`. Written, **not run**.

## Remaining work

1. The last commit (a8829cd) is an untested WIP of the wiring (Pane double-click dispatch,
   `Dialogs.jsx`, `frontend_views.py`, and it deleted `utils/documentKinds.js` /
   `utils/safeLinks.js` while refactoring). Review it first: make the build pass
   (`npm run build`) and all unit tests pass (backend, `bin/ci/frontend_unittest.sh`).
2. Merge the latest `dirk`, keep a single Alembic head (the branch adds no migration).
3. Finish the PDF viewer checks the agent was on: find (Ctrl+F), page navigation, zoom
   (fit width / fit page / %), internal links; scripting off (`isEvalSupported: false`,
   no `enableScripting`), links only http/https/mailto with `rel="noopener noreferrer"`.
4. Check DOCX safety with the fixtures: a `javascript:` hyperlink is not clickable, remote
   images are not loaded, altChunk HTML is not rendered or is sanitized.
5. XLSX: sheet tabs, values only (no formulas, no macros), first 5,000 rows x 200 columns
   with a notice when cut off.
6. Legacy `.doc` / `.ppt` and other unsupported types: "No preview for this file type".
7. Bundle: the main bundle must not grow; each renderer is a lazy chunk. Record the sizes.
8. Run `MOTUZ_E2E_UI=require test/e2e/run.sh` (full). Save screenshots of each viewer with
   `MOTUZ_E2E_SCREENSHOTS`.
9. Short CLAUDE.md paragraph next to the viewer section (endpoint, caps, libraries,
   security rules), README if it documents the viewer.
