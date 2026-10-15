import { useEffect, useState } from "react";
import { api, setExaminer, SIGNED_OUT, urls } from "./api";
import { AccountMenu } from "./components/AccountMenu";
import { CaseList } from "./components/CaseList";
import { JobQueue } from "./components/JobQueue";
import { SignIn } from "./components/SignIn";
import { Workspace } from "./components/Workspace";
import type { AnalysisSchema, FilterSchema, User } from "./types";

function readHash() {
  const m = location.hash.match(/^#\/case\/([\w-]+)/);
  return m ? m[1] : null;
}

export default function App() {
  const [me, setMe] = useState<User | null>(null);
  const [setupRequired, setSetupRequired] = useState<boolean | null>(null);
  const [caseId, setCaseId] = useState<string | null>(readHash());
  const [filters, setFilters] = useState<FilterSchema[]>([]);
  const [analyses, setAnalyses] = useState<AnalysisSchema[]>([]);
  const [caseName, setCaseName] = useState("");

  const signedIn = (u: User | null) => {
    setExaminer(u?.full_name ?? "");
    setMe(u);
  };

  const checkSession = () =>
    api
      .authStatus()
      .then((s) => {
        setSetupRequired(s.setup_required);
        signedIn(s.user);
      })
      .catch(() => setSetupRequired(false));

  useEffect(() => {
    checkSession();
    const onHash = () => setCaseId(readHash());
    const onSignedOut = () => signedIn(null);
    window.addEventListener("hashchange", onHash);
    window.addEventListener(SIGNED_OUT, onSignedOut);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener(SIGNED_OUT, onSignedOut);
    };
  }, []);

  useEffect(() => {
    if (!me) return;
    api.filters().then(setFilters);
    api.analyses().then(setAnalyses);
  }, [me?.username]);

  useEffect(() => {
    if (me && caseId) api.caseDetail(caseId).then((d) => setCaseName(d.case.name)).catch(() => setCaseName(""));
  }, [caseId, me?.username]);

  if (setupRequired === null) return <p className="loading muted">Loading…</p>;
  if (!me) {
    return (
      <SignIn
        setup={setupRequired}
        onSignedIn={(u) => {
          setSetupRequired(false);
          signedIn(u);
        }}
      />
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <a href="#/" className="brand">Forensic Workbench</a>
        {caseId && (
          <>
            <span className="crumb">/ {caseName}</span>
            <span className="spacer" />
            <span className="muted small">Report:</span>
            <a className="button" href={urls.report(caseId, "html")} target="_blank" rel="noreferrer">HTML</a>
            <a className="button" href={urls.report(caseId, "pdf")} target="_blank" rel="noreferrer">PDF</a>
            <a className="button" href={urls.report(caseId, "json")} target="_blank" rel="noreferrer">Bundle</a>
          </>
        )}
        {!caseId && <span className="spacer" />}
        <JobQueue me={me} />
        <AccountMenu me={me} onSignedOut={() => signedIn(null)} />
      </header>
      {caseId ? (
        <Workspace key={caseId} caseId={caseId} filters={filters} analyses={analyses} />
      ) : (
        <CaseList onOpen={(id) => (location.hash = `#/case/${id}`)} />
      )}
    </div>
  );
}
