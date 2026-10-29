interface BrandLogoProps {
  size?: number
  className?: string
}

/**
 * The app's mark.
 *
 * An image rather than inline SVG: the artwork is multicolour, so there is
 * nothing for `currentColor` to do, and a raster asset keeps the gradients
 * and glow the SVG path could not carry. It is served from `public/brand`
 * and cached by the browser like any other static asset.
 *
 * `size` sets both dimensions — the source is square, and every call site
 * wants it square.
 */
export function BrandLogo({ size = 24, className }: BrandLogoProps) {
  return (
    <img
      src="/brand/logo.png"
      width={size}
      height={size}
      // Decorative everywhere it is used: the product name is always next to
      // it in text, so announcing it again is noise for a screen reader.
      alt=""
      aria-hidden="true"
      draggable={false}
      className={className}
      style={{ width: size, height: size, objectFit: 'contain' }}
    />
  )
}
