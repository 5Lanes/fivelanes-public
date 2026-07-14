export function setUnreadBadgeCount(count) {
    window.webkit?.messageHandlers?.fivelanesSetBadge?.postMessage({ count });
}
