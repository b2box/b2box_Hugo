import { useState } from "react";
import { isUnreachableImage } from "../lib/format";

// Thumbnail 64x64 con fallback diagonal (patrón .thumb-fallback) cuando no hay
// URL, es una URL de demo inalcanzable, o la imagen falla al cargar.
export default function Thumb({
  imageUrl,
  alt,
}: {
  imageUrl?: string | null;
  alt?: string | null;
}) {
  const [failed, setFailed] = useState(false);
  const url = (imageUrl || "").trim();
  const usable = url && !isUnreachableImage(url) && !failed;

  if (usable) {
    return (
      <img
        src={url}
        alt={alt || ""}
        loading="lazy"
        onError={() => setFailed(true)}
        className="w-16 h-16 rounded-lg object-cover bg-muted shrink-0"
      />
    );
  }
  return <div className="w-16 h-16 rounded-lg thumb-fallback shrink-0" />;
}
