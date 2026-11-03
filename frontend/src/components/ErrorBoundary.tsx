import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * Last line of defence: if one screen throws while rendering, the person sees what happened and a way out,
 * and the top bar stays usable. Data and network errors are handled where they happen (Boundary, ActionError);
 * this only catches programming mistakes. React still needs a class for this.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Screen crashed:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="page page--narrow">
        <div className="errorbox" role="alert">
          <p className="errorbox__title">This screen hit a problem</p>
          <p className="errorbox__body">The rest of the app still works. Reload the screen; if it happens again, tell us what you clicked.</p>
          <p className="errorbox__meta">
            <span className="num">{this.state.error.message}</span>
          </p>
          <button type="button" className="btn" onClick={() => this.setState({ error: null })}>
            Reload this screen
          </button>
        </div>
      </div>
    );
  }
}
