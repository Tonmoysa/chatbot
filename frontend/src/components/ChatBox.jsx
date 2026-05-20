import { useEffect, useRef } from "react";
import MessageBubble from "./MessageBubble";
import ChatInput from "./chat/ChatInput";
import TypingIndicator from "./chat/TypingIndicator";
import ChatThreadHeader from "./chat/ChatThreadHeader";
import DateSeparator from "./chat/DateSeparator";
import { getDisplayDate } from "./chat/dateUtils.js";
import "../styles/chat-design.css";

/**
 * WhatsApp-style chat shell (design system under .hr-chat-card).
 * Preserves existing props: messages, loading, error, onSend, onClearError.
 */
export default function ChatBox({ messages, loading, error, onSend, onClearError }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, loading]);

  const showEmpty = messages.length === 0 && !loading;

  const renderThread = () => {
    let lastDate = null;
    const elements = [];

    messages.forEach((msg, i) => {
      const msgDate = getDisplayDate(msg.at);
      if (msgDate !== lastDate) {
        elements.push(<DateSeparator key={`sep-${msg.at || i}-${msgDate}`} date={msgDate} />);
        lastDate = msgDate;
      }
      elements.push(
        <MessageBubble
          key={`${i}-${msg.role}-${(msg.text || "").slice(0, 24)}`}
          message={msg}
        />
      );
    });

    return elements;
  };

  return (
    <div className="hr-chat-card">
        <ChatThreadHeader />

        <div className="chat-body chat-scroll" role="log" aria-live="polite" aria-relevant="additions">
          {showEmpty ? (
            <div className="chat-empty-greeting">
              <h2 className="chat-empty-greeting-title">How can I help you today?</h2>
            </div>
          ) : (
            renderThread()
          )}

          {loading ? <TypingIndicator /> : null}

          <span ref={bottomRef} className="block h-px w-full shrink-0" aria-hidden />
        </div>

        <ChatInput onSend={onSend} disabled={loading} error={error} onClearError={onClearError} />
    </div>
  );
}
