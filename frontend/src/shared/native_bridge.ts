declare global {
  interface Window {
    webkit?: {
      messageHandlers?: {
        fivelanesSetBadge?: { postMessage(payload: unknown): void };
      };
    };
  }
}

export function setUnreadBadgeCount(count: number): void {
  window.webkit?.messageHandlers?.fivelanesSetBadge?.postMessage({ count });
}
