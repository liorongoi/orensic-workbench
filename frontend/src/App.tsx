import { useEffect, useState } from "react";
import { api, getExaminer, setExaminer, urls } from "./api";
import { CaseList } from "./components/CaseList";
import { Workspace } from "./components/Workspace";
import type { AnalysisSchema, FilterSchema } from "./types";
import logo from "./assets/lucida.svg";

function readHash() {
  const m = location.hash.match(/^#\/case\/([\w-]+)/);
  return m ? m[1] : null;
}

export default function App() {
  const [examiner, setName] = useState(getExaminer());
  const [draftName, setDraftName] = useState("");
  const [caseId, setCaseId] = useState<string | null>(readHash());
  const [filters, setFilters] = useState<FilterSchema[]>([]);
  const [analyses, setAnalyses] = useState<AnalysisSchema[]>([]);
  const [caseName, setCaseName] = useState("");

  useEffect(() => {
    api.filters().then(setFilters);
    api.analyses().then(setAnalyses);
    const onHash = () => setCaseId(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (caseId) api.caseDetail(caseId).then((d) => setCaseName(d.case.name)).catch(() => setCaseName(""));
  }, [caseId]);

  if (!examiner) {
    return (
      <div className="signin">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!draftName.trim()) return;
            setExaminer(draftName.trim());
            setName(draftName.trim());
          }}
        >
          <h1 className="signin-brand">
            <img src={logo} alt="" width={40} height={40} />
            <span className="wordmark">Lucida</span>
          </h1>
          <p className="muted">Your actions are logged under this name.</p>
          <input autoFocus placeholder="Examiner name" value={draftName} onChange={(e) => setDraftName(e.target.value)} />
          <button className="primary">Continue</button>
        </form>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <a href="#/" className="brand">
          <img src={logo} alt="" width={22} height={22} />
          <span className="wordmark">Lucida</span>
        </a>
        {caseId && (
          <>
            <span className="crumb">/ {caseName}</span>
            <span className="spacer" />
            <details className="menu">
              <summary className="button">Report ▾</summary>
              <div className="menu-items">
                <a href={urls.report(caseId, "html")} target="_blank" rel="noreferrer">HTML report</a>
                <a href={urls.report(caseId, "pdf")} target="_blank" rel="noreferrer">PDF report</a>
                <a href={urls.report(caseId, "json")} target="_blank" rel="noreferrer" title="Machine-readable bundle with every hash">JSON bundle</a>
              </div>
            </details>
          </>
        )}
        {!caseId && <span className="spacer" />}
        <span className="examiner" title="Recorded as the actor in the audit log">
          {examiner}
          <button
            className="link"
            onClick={() => {
              setExaminer("");
              setName("");
            }}
          >
            change
          </button>
        </span>
      </header>
      {caseId ? (
        <Workspace key={caseId} caseId={caseId} filters={filters} analyses={analyses} />
      ) : (
        <CaseList onOpen={(id) => (location.hash = `#/case/${id}`)} />
      )}
    </div>
  );
}
