"""Prototype: find the white symbol cards on a dice scene."""
import pathlib
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw


def cards(path: pathlib.Path):
    bgr = cv2.imread(str(path))
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    # cards are near-white: bright and barely saturated
    v = hsv[:, :, 2]
    s = hsv[:, :, 1]
    mask = ((v > 185) & (s < 45)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < 120 or area > 5000:
            continue
        peri = cv2.arcLength(c, True)
        quad = cv2.approxPolyDP(c, 0.04 * peri, True)
        if len(quad) != 4:
            continue
        quad = quad.reshape(-1, 2).astype(np.float32)
        out.append((area, quad))
    out.sort(key=lambda t: -t[0])
    return bgr, out


def order(q):
    s = q.sum(axis=1)
    d = np.diff(q, axis=1).ravel()
    return np.array([q[np.argmin(s)], q[np.argmin(d)], q[np.argmax(s)], q[np.argmax(d)]], np.float32)


def main():
    path = pathlib.Path(sys.argv[1])
    bgr, found = cards(path)
    print(path, "cards:", len(found))
    tiles = []
    for area, quad in found:
        box = order(quad)
        side = 56
        dst = np.array([[0, 0], [side - 1, 0], [side - 1, side - 1], [0, side - 1]], np.float32)
        warp = cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(box, dst), (side, side))
        tiles.append((area, box, warp))
        cv2.polylines(bgr, [box.astype(np.int32)], True, (0, 255, 0), 1)

    cv2.imwrite("/tmp/cards_marked.png", bgr)
    cols = 8
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * 70, max(1, rows) * 70), (20, 20, 20))
    dr = ImageDraw.Draw(sheet)
    for i, (area, box, warp) in enumerate(tiles):
        im = Image.fromarray(cv2.cvtColor(warp, cv2.COLOR_BGR2RGB))
        sheet.paste(im, ((i % cols) * 70 + 4, (i // cols) * 70 + 4))
        dr.text(((i % cols) * 70 + 4, (i // cols) * 70 + 56), str(int(area)), fill=(255, 255, 0))
    sheet.save("/tmp/cards_sheet.png")
    print("wrote /tmp/cards_marked.png /tmp/cards_sheet.png")


if __name__ == "__main__":
    main()
