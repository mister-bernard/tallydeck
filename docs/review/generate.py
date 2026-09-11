from pathlib import Path
from PIL import Image, ImageDraw
from tallydeck.render.keycard import draw_key
from tallydeck.render import theme
from tallydeck.signal import Signal
from tallydeck.brief import render
from tallydeck.popup import Console,THEME
import sys
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
rows=[('claude','A','attention','Site review'),('codex','O','blocked','Release check'),('codex','O2','working','Garden planner'),('grok','X','attention','Cruiser Finder')]
canvas=Image.new('RGB',(840,520),'#101114');d=ImageDraw.Draw(canvas)
d.text((20,12),'Attention that fits. One press to act.',font=theme.font('display',26),fill='#f2f3f5')
for n,(h,a,state,label) in enumerate(rows):
 sig=Signal(id='demo/'+a,label=label,state=state,sublabel='Which region should the next search cover?',progress=.62 if state=='working' else None,meta={'harness':h,'account':a})
 for row,lit in enumerate([False,True]):
  face=draw_key(sig,96,lit=lit,askpage=0).resize((192,192))
  canvas.paste(face,(20+n*204,58+row*224))
 d.text((22+n*204,478),h.title()+' '+a,font=theme.font('regular',17),fill='#b8bbc3')
canvas.save(out/'operator-keys.png')
doc={'label':'Cruiser Finder','state':'attention','project':'Search ready for review','steps':{'1':'Review the shortlist','2':'Refine the search area','3':'Keep watching for new matches'},'sections':[('SUMMARY','The latest search is complete. Three candidates match the current filters. The full comparison is ready to review.'),('NEXT STEPS','Choose the next direction above, or press **i** to write a reply. Press **Enter** to open the session.'),('WHAT HAPPENED','The latest listings were checked against the existing requirements. Duplicate results were folded into the shortlist.') ]}
c=Console(record=True,width=96,theme=THEME,color_system='truecolor')
c.print(render(doc,96));c.print('\n1/2/3 send choice · i reply · Enter open · x park · space mute · q dismiss')
c.save_svg(str(out/'operator-popup.svg'),title='Tallydeck — session brief')
