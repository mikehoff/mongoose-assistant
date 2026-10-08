// Fortune cookie card renderer. Draws an A5 LANDSCAPE PDF: cream background, navy bold serif text,
// vertically + horizontally centred, with full colour emoji (via Core Text / Apple Color Emoji).
// Garamond is preferred; if it is not installed the renderer falls back through a serif chain to Times
// New Roman. A bundled font file can be passed as arg 3 (registered at runtime) so dropping an EB
// Garamond .ttf into the assets font dir makes the card true Garamond with no system install.
//
//   swift fortune_card.swift <out.pdf> <text> [optional_font_file.ttf]
//
// Prints the chosen PostScript font name to stdout (so the caller can log what was actually used).
import AppKit
import CoreText
import Foundation

let args = CommandLine.arguments
guard args.count >= 3 else { FileHandle.standardError.write("usage: <out> <text> [font]\n".data(using: .utf8)!); exit(2) }
let outPath = args[1]
let text = args[2]
let fontFile = args.count >= 4 ? args[3] : ""

// Register a bundled font file if one was supplied and exists.
if !fontFile.isEmpty, FileManager.default.fileExists(atPath: fontFile) {
    CTFontManagerRegisterFontsForURL(URL(fileURLWithPath: fontFile) as CFURL, .process, nil)
}

let W: CGFloat = 595.28, H: CGFloat = 419.53   // A5 landscape (210 x 148 mm)
var media = CGRect(x: 0, y: 0, width: W, height: H)
guard let consumer = CGDataConsumer(url: URL(fileURLWithPath: outPath) as CFURL),
      let ctx = CGContext(consumer: consumer, mediaBox: &media, nil) else {
    FileHandle.standardError.write("pdf context failed\n".data(using: .utf8)!); exit(1)
}
ctx.beginPDFPage(nil)

// Cream background.
let cream = NSColor(calibratedRed: 0.988, green: 0.961, blue: 0.867, alpha: 1)
ctx.setFillColor(cream.cgColor)
ctx.fill(media)

// Font: Garamond chain -> Times New Roman -> system serif, always bold.
func pickFont(_ size: CGFloat) -> NSFont {
    for n in ["Garamond", "EBGaramond-Regular", "EB Garamond", "ITC Garamond", "AGaramondPro-Regular",
              "Times New Roman"] {
        if let f = NSFont(name: n, size: size) {
            return NSFontManager.shared.convert(f, toHaveTrait: .boldFontMask)
        }
    }
    return NSFont.boldSystemFont(ofSize: size)
}
let font = pickFont(32)
let navy = NSColor(calibratedRed: 0.06, green: 0.11, blue: 0.32, alpha: 1)
let para = NSMutableParagraphStyle()
para.alignment = .center
para.lineSpacing = 8
let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: navy, .paragraphStyle: para]
let astr = NSAttributedString(string: text, attributes: attrs)

// Horizontal padding, then vertical centring from the measured text height.
let pad: CGFloat = 46
let textW = W - 2 * pad
let bounds = astr.boundingRect(with: CGSize(width: textW, height: .greatestFiniteMagnitude),
                               options: [.usesLineFragmentOrigin, .usesFontLeading])
let textH = ceil(bounds.height)
let originY = (H - textH) / 2

let nsctx = NSGraphicsContext(cgContext: ctx, flipped: false)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = nsctx
astr.draw(with: CGRect(x: pad, y: originY, width: textW, height: textH),
          options: [.usesLineFragmentOrigin, .usesFontLeading])
NSGraphicsContext.restoreGraphicsState()

ctx.endPDFPage()
ctx.closePDF()
print(font.fontName)
