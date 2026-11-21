import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { ApiError, fetchSingAllJob } from "../api";
import "./ProtectedRoute.css";

type LinkState = "opening" | "not_yours" | "failed";

/**
 * The link in a Sing All's ended email: opens the job's Song on its card.
 * Another user's job and a missing one answer the same 404, so both read "not yours".
 */
export default function JobLinkPage() {
  const { jobId = "" } = useParams();
  const navigate = useNavigate();
  const [state, setState] = useState<LinkState>("opening");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setState("opening");
    fetchSingAllJob(jobId)
      .then((job) => {
        if (!live) return;
        navigate(
          `/app/song/${encodeURIComponent(job.songId)}?job=${encodeURIComponent(job.jobId)}`,
          { replace: true },
        );
      })
      .catch((error: unknown) => {
        if (!live) return;
        setState(error instanceof ApiError && error.status === 404 ? "not_yours" : "failed");
      });
    return () => {
      live = false;
    };
  }, [jobId, navigate, attempt]);

  if (state === "opening") {
    return (
      <div className="protected-route-loading">
        <Loader2 className="protected-route-spinner" size={32} />
        <p>Opening your song…</p>
      </div>
    );
  }
  return (
    <div className="protected-route-loading" data-testid="job-link-message">
      {state === "not_yours" ? (
        <p role="alert">This job isn't yours. Ask the owner for a share link.</p>
      ) : (
        <>
          <p role="alert">This job couldn't be opened.</p>
          <button type="button" className="sing-all-link" onClick={() => setAttempt((value) => value + 1)}>
            Try again
          </button>
        </>
      )}
      <Link to="/app">Go to SightSinger</Link>
    </div>
  );
}
