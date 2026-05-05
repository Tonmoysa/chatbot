export default function MessageBubble({ role, text }) {
  const isUser = role === "user";

  return (
    <div
      className={`flex w-full animate-fade-in ${isUser ? "justify-end" : "justify-start"}`}
      role="article"
      aria-label={isUser ? "You" : "Assistant"}
    >
      <div
        className={`max-w-[min(85%,42rem)] rounded-2xl px-4 py-2.5 text-[15px] leading-relaxed shadow-sm transition-shadow ${
          isUser
            ? "bg-emerald-600 text-white dark:bg-emerald-700"
            : "bg-white text-slate-800 ring-1 ring-slate-200/80 dark:bg-slate-900 dark:text-slate-100 dark:ring-slate-700"
        }`}
      >
        <p className="whitespace-pre-wrap break-words">{text}</p>
      </div>
    </div>
  );
}
