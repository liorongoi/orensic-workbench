import { lazy, Suspense, useEffect, useState } from "react";
import { api, setExaminer } from "./api";
import { CaseList } from "./components/CaseList";
import { JobQueue } from "./components/JobQueue";
import { ExaminerMenu } from "./components/ExaminerMenu";
import { MenuBar } from "./components/MenuBar";
import { Workspace } from "./components/Workspace";
import type { AnalysisSchema, FilterSchema, User } from "./types";
import logo from "./assets/lucida.svg";
import { readHelpHash } from "./help";

const HelpPage = lazy(() => import("./components/HelpPage"));

function readHash() {
  const m = location.hash.match(/^#\/case\/([\w-]+)/);
  return m ? m[1] : null;
}

/** `#/case/<id>?ev=<evidence>&layout=viewer`: a second window showing only the viewer. */
function readHashOptions() {
  const q = new URLSearchParams(location.hash.split("?")[1] ?? "");
  return { evidence: q.get("ev"), viewerOnly: q.get("layout") === "viewer" };
}

export default function App() {
  const [me, setMe] = useState<User | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [caseId, setCaseId] = useState<string | null>(readHash());
  const [filters, setFilters] = useState<FilterSchema[]>([]);
  const [analyses, setAnalyses] = useState<AnalysisSchema[]>([]);
  const [caseName, setCaseName] = useState("");
  const [help, setHelp] = useState(readHelpHash() !== null);

  const named = (u: User) => {
    setExaminer(u.full_name);
    setMe(u);
  };

  useEffect(() => {
    // No sign-in: the app opens straight away as the built-in local examiner.
    api.me().then(named).catch((e) => setLoadError((e as Error).message));
    const onHash = () => {
      setCaseId(readHash());
      setHelp(readHelpHash() !== null);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!me) return;
    api.filters().then(setFilters);
    api.analyses().then(setAnalyses);
  }, [me?.username]);

  useEffect(() => {
    if (me && caseId) api.caseDetail(caseId).then((d) => setCaseName(d.case.name)).catch(() => setCaseName(""));
  }, [caseId, me?.username]);

  // Help opens on its own: the guides are part of the app.
  if (help) {
    return (
      <div className="app">
        <header className="topbar">
          <a href="#/" className="brand">
            <img src={logo} alt="" width={22} height={22} />
            <span className="wordmark">Lucida</span>
          </a>
          <MenuBar caseId={null} signedIn={!!me} />
          <span className="crumb">/ Help</span>
          <span className="spacer" />
          <a className="button" href="#/">Back to Lucida</a>
        </header>
        <Suspense fallback={<p className="loading muted">Loading…</p>}>
          <HelpPage />
        </Suspense>
      </div>
    );
  }

  if (!me) return <p className="loading muted">{loadError ? `Lucida could not start: ${loadError}` : "Loading…"}</p>;

  return (
    <div className="app">
      <header className="topbar">
        <a href="#/" className="brand">
          <img src={logo} alt="" width={22} height={22} />
          <span className="wordmark">Lucida</span>
        </a>
        <MenuBar caseId={caseId} signedIn />
        {caseId && <span className="crumb">/ {caseName}</span>}
        <span className="spacer" />
        <JobQueue me={me} />
        <ExaminerMenu me={me} onRenamed={named} />
      </header>
      {caseId ? (
        <Workspace key={caseId} caseId={caseId} filters={filters} analyses={analyses} {...readHashOptions()} />
      ) : (
        <CaseList onOpen={(id) => (location.hash = `#/case/${id}`)} />
      )}
    </div>
  );
}
