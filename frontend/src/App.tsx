import { lazy, Suspense, useEffect, useState } from "react";
import { api, setExaminer, SIGNED_OUT } from "./api";
import { AccountMenu } from "./components/AccountMenu";
import { CaseList } from "./components/CaseList";
import { JobQueue } from "./components/JobQueue";
import { MenuBar } from "./components/MenuBar";
import { SignIn } from "./components/SignIn";
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
  const [setupRequired, setSetupRequired] = useState<boolean | null>(null);
  const [caseId, setCaseId] = useState<string | null>(readHash());
  const [filters, setFilters] = useState<FilterSchema[]>([]);
  const [analyses, setAnalyses] = useState<AnalysisSchema[]>([]);
  const [caseName, setCaseName] = useState("");
  const [help, setHelp] = useState(readHelpHash() !== null);

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
    const onHash = () => {
      setCaseId(readHash());
      setHelp(readHelpHash() !== null);
    };
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

  // Help needs no sign-in: the guides are part of the app.
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
        <a href="#/" className="brand">
          <img src={logo} alt="" width={22} height={22} />
          <span className="wordmark">Lucida</span>
        </a>
        <MenuBar caseId={caseId} signedIn />
        {caseId && <span className="crumb">/ {caseName}</span>}
        <span className="spacer" />
        <JobQueue me={me} />
        <AccountMenu me={me} onSignedOut={() => signedIn(null)} />
      </header>
      {caseId ? (
        <Workspace key={caseId} caseId={caseId} filters={filters} analyses={analyses} {...readHashOptions()} />
      ) : (
        <CaseList onOpen={(id) => (location.hash = `#/case/${id}`)} />
      )}
    </div>
  );
}
