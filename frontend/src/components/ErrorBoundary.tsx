import { Component, type ErrorInfo, type ReactNode } from "react";

/** A broken view never leaves a blank window: it says so calmly and offers a reload. */
export default class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="boot">
        <span className="mark still" aria-hidden="true" />
        <p>
          Something went wrong in this window.
          <br />
          Your tasks and the phone carry on.
        </p>
        <button className="btn" onClick={() => location.reload()}>
          Reload
        </button>
        {import.meta.env.DEV && <pre className="boot-error">{error.stack || String(error)}</pre>}
      </div>
    );
  }
}
