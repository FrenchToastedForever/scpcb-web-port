Global ark_blur_image%, ark_blur_texture%, ark_sw%, ark_sh%, ark_tex%
Global ark_blur_cam%

Function CreateBlurImage()
	;Create blur Camera
	Local cam% = CreateCamera()
	CameraProjMode cam,2
	CameraZoom cam,0.1
	CameraClsMode cam, 0, 0
	CameraRange cam, 0.1, 1.5
	MoveEntity cam, 0, 0, 10000
	EntityOrder cam, -1
	ark_blur_cam = cam
	
	ark_sw = GraphicWidth;GraphicsWidth()
	ark_sh = GraphicHeight;GraphicsHeight()
	CameraViewport cam,0,0,ark_sw,ark_sh

	; Hard-cap the blur render target at 512x512. Growing this with the screen
	; resolution (up to 2048/4096) exhausted VRAM and crashed ChromeOS drivers.
	ark_tex = 512

	;Create sprite
	Local spr% = CreateMesh(cam)
	Local sf% = CreateSurface(spr)
	AddVertex sf, -1, 1, 0, 0, 0
	AddVertex sf, 1, 1, 0, 1, 0
	AddVertex sf, -1, -1, 0, 0, 1
	AddVertex sf, 1, -1, 0, 1, 1
	AddTriangle sf, 0, 1, 2
	AddTriangle sf, 3, 2, 1
	EntityFX spr, 17
	; Cover the full viewport regardless of the smaller blur target.
	ScaleEntity spr, 1.0, Float(GraphicHeight) / Float(GraphicWidth), 1
	PositionEntity spr, 0, 0, 1.0001
	EntityOrder spr, -100000
	EntityBlend spr, 1
	ark_blur_image = spr
	
	;Create blur texture
	ark_blur_texture = CreateTexture(ark_tex, ark_tex, 256)
	EntityTexture spr, ark_blur_texture
End Function

Function UpdateBlur(power#)
	
	EntityAlpha ark_blur_image, power#
	
	; Copy a centred, clamped region so the 512x512 target never goes negative
	; or out of bounds at higher screen resolutions.
	Local cw% = Min(GraphicWidth, ark_tex)
	Local ch% = Min(GraphicHeight, ark_tex)
	CopyRect (GraphicWidth - cw) / 2, (GraphicHeight - ch) / 2, cw, ch, (ark_tex - cw) / 2, (ark_tex - ch) / 2, BackBuffer(), TextureBuffer(ark_blur_texture)
	
End Function

;~IDEal Editor Parameters:
;~F#3#26
;~C#Blitz3D