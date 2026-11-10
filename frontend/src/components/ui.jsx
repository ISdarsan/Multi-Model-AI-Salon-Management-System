// Small building blocks used on every screen, so the whole dashboard looks the same.

// A white rounded box.
export function Card({ className = '', children }) {
  return <div className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`}>{children}</div>
}

// Page title with an optional line of help text underneath.
export function PageHeader({ title, subtitle, right }) {
  return (
    <div className="mb-6 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </div>
      {right}
    </div>
  )
}

// Shown while data is loading.
export function Loading({ text = 'Loading...' }) {
  return (
    <div className="flex items-center gap-3 p-8 text-slate-500">
      <span className="h-5 w-5 animate-spin rounded-full border-2 border-slate-300 border-t-indigo-600" />
      {text}
    </div>
  )
}

// Shown when something went wrong. `onRetry` adds a "Try again" button.
export function ErrorBox({ message, onRetry }) {
  return (
    <div className="mb-4 flex items-start justify-between gap-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
      <div>
        <p className="font-medium">Something went wrong</p>
        <p className="mt-1">{message}</p>
      </div>
      {onRetry && (
        <button onClick={onRetry} className="shrink-0 rounded-md border border-red-300 bg-white px-3 py-1 font-medium hover:bg-red-100">
          Try again
        </button>
      )}
    </div>
  )
}

// Shown when there is nothing to list.
export function Empty({ title, text }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
      <p className="font-medium text-slate-700">{title}</p>
      {text && <p className="mt-1 text-sm text-slate-500">{text}</p>}
    </div>
  )
}

// A small coloured label.
const BADGE_COLOURS = {
  low: 'bg-emerald-100 text-emerald-800',
  medium: 'bg-amber-100 text-amber-800',
  high: 'bg-red-100 text-red-800',
  neutral: 'bg-slate-100 text-slate-700',
  indigo: 'bg-indigo-100 text-indigo-800',
}
export function Badge({ tone = 'neutral', children }) {
  return <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${BADGE_COLOURS[tone]}`}>{children}</span>
}

// Buttons. `variant` is 'primary' (indigo), 'danger' (red outline) or 'plain' (grey).
const BUTTON_STYLES = {
  primary: 'bg-indigo-600 text-white hover:bg-indigo-700',
  danger: 'border border-red-300 bg-white text-red-700 hover:bg-red-50',
  plain: 'border border-slate-300 bg-white text-slate-700 hover:bg-slate-50',
}
export function Button({ variant = 'plain', className = '', ...props }) {
  return (
    <button
      {...props}
      className={`rounded-lg px-4 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${BUTTON_STYLES[variant]} ${className}`}
    />
  )
}
