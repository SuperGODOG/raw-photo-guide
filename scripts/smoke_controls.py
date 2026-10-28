# /// script
# dependencies = ["Pillow", "numpy"]
# ///
import argparse,json,tempfile,time
from pathlib import Path
import numpy as np
from PIL import Image,ImageCms
import rt_tool as rt
def main():
    parser=argparse.ArgumentParser(description='Real RT 5.13 common-control smoke test using a synthetic JPEG; creates new files only.')
    parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args()
    args.output_dir=args.output_dir.expanduser().resolve();args.output_dir.mkdir(parents=True,exist_ok=True)
    root=Path(tempfile.mkdtemp(prefix='common-controls-',dir=args.output_dir))
    x,y=np.meshgrid(np.linspace(0,1,960),np.linspace(0,1,640))
    a=np.stack([x*.65+y*.25, .05+y*.8, .05+(1-x)*.7],axis=-1)
    # Add texture, black-to-white ramps, six hue patches, and an asymmetric marker.
    a+=np.sin(x[:,:,None]*240)*np.cos(y[:,:,None]*180)*.035
    a[:100]=np.repeat(x[:100,:,None],3,axis=2)
    a[150:220,40:120]=[.8,.1,.1];a[150:220,180:260]=[.8,.8,.1];a[150:220,320:400]=[.1,.8,.1]
    a[150:220,460:540]=[.1,.8,.8];a[150:220,600:680]=[.1,.1,.8];a[150:220,740:820]=[.8,.1,.8]
    a[450:600,70:200]=[.85,.5,.2]
    source=root/'测试 图像.jpg';icc=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes();Image.fromarray(np.uint8(np.clip(a,0,1)*255)).save(source,quality=97,icc_profile=icc)
    before=rt.digest(source);meta,_=rt.profile('builtin:neutral-rec2020-rt513');engine=rt.engine()
    req={'action':'render','source':str(source),'profile':meta['path'],'profile_sha256':meta['sha256'],'engine_version':engine['version'],'output_dir':str(root/'output'),'format':'tiff16','threads':2}
    cases={'neutral':{},'shadows':{'shadows_highlights':{'shadows':65}},'highlights':{'shadows_highlights':{'highlights':65}},'vibrance':{'vibrance':{'pastels':60,'saturated':10,'protect_skin':True}},'local_contrast':{'local_contrast':{'amount':.8,'radius':25}},'gradient':{'graduated_filter':{'strength':1,'degree':0,'feather':20,'center_x':0,'center_y':0}},'black_white':{'black_white':{'enabled':True}},'rotate':{'transform':{'rotate':90}},'flip':{'transform':{'horizontal_flip':True}},'straighten':{'transform':{'straighten':2,'autofill':True}},'perspective':{'perspective':{'vertical':15,'horizontal':-10}},'resize':{'resize':{'long_edge':320}},'hsv_hue':{'hsv_equalizer':{'hue_curve':[[0,.7],[1/3,.5],[2/3,.5]]}},'hsv_saturation':{'hsv_equalizer':{'saturation_curve':[[0,.2],[1/3,.5],[2/3,.5]]}},'hsv_value':{'hsv_equalizer':{'value_curve':[[0,.7],[1/3,.5],[2/3,.5]]}}}
    results={};arrays={}
    for name,adj in cases.items():
     t=time.monotonic();res=rt.dispatch({**req,'adjustments':adj});im=np.array(Image.open(res['output']).convert('RGB'),dtype=float)
     arrays[name]=im
     delta=float(np.abs(im-arrays['neutral']).mean()) if im.shape==arrays['neutral'].shape else None
     results[name]={'manifest':res['manifest'],'output':res['output'],'width':res['qc']['width'],'height':res['qc']['height'],'elapsed':round(time.monotonic()-t,3),'mean_abs_delta_8bit_display':delta}
     print(name,results[name],flush=True)
     if name not in ('neutral','rotate','resize'):assert delta is not None and delta>.01,(name,delta)
     if name=='resize':assert max(im.shape[:2])==320
     if name=='rotate':assert im.shape[:2]==(960,640)
     if name=='black_white':assert np.max(np.abs(im[:,:,0]-im[:,:,1]))<=1 and np.max(np.abs(im[:,:,1]-im[:,:,2]))<=1
    # Crop uses the full-size manifest. Reference hash and canonical path are auto-bound.
    crop={'crop':{'x':60,'y':100,'width':480,'height':320,'reference_manifest':results['neutral']['manifest']}}
    for name,adj in [('crop',crop),('crop_resize',{**crop,'resize':{'long_edge':240}})]:
     res=rt.dispatch({**req,'adjustments':adj});size=(res['qc']['width'],res['qc']['height']);assert size==((480,320) if name=='crop' else (240,160)),size
     results[name]={'manifest':res['manifest'],'output':res['output'],'size':size};print(name,size,flush=True)
    # Alignment assertions use unchanged pixels where possible; allow minor re-encoding/engine rounding.
    assert np.mean(np.abs(arrays['flip']-arrays['neutral'][:,::-1]))<1
    assert np.mean(np.abs(arrays['rotate']-np.rot90(arrays['neutral'],k=-1)))<1
    base=arrays['neutral'];grad=arrays['gradient'];top=float((base[:80]-grad[:80]).mean());bottom=float((base[-80:]-grad[-80:]).mean());assert top>bottom+5,(top,bottom)
    assert rt.digest(source)==before
    report={'engine':engine,'source_sha256':before,'source_fixture':'synthetic JPEG, real RawTherapee CLI; not a real RAW acceptance test','results':results,'gradient_top_darkening':top,'gradient_bottom_darkening':bottom,'source_unchanged':True}
    (root/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print('REPORT',root/'verification.json',flush=True)

if __name__ == "__main__":
    main()
