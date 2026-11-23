import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

let scene, camera, renderer, controls, currentRoot, placeholderGroup, pbrMesh;
let lastPbrArgs = null;
let pbrShape = "sphere";
const container = document.getElementById("viewer3d");

export function init3D() {
  if (scene) return;
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d0e10);
  camera = new THREE.PerspectiveCamera(45, container.clientWidth / Math.max(1, container.clientHeight), 0.05, 100);
  camera.position.set(2.4, 2.0, 2.8);

  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(container.clientWidth, Math.max(1, container.clientHeight));
  container.appendChild(renderer.domElement);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.target.set(0, 0.9, 0);

  scene.add(new THREE.AmbientLight(0xffffff, 0.9));
  const key = new THREE.DirectionalLight(0xfff4e0, 1.8);
  key.position.set(3.5, 5.0, 4.0);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xb8c6e0, 0.75);
  rim.position.set(-4.0, 2.5, -3.5);
  scene.add(rim);

  const grid = new THREE.GridHelper(6, 12, 0x2a2d33, 0x1c1e24);
  scene.add(grid);

  placeholderGroup = new THREE.Group();
  const outerGeo = new THREE.BoxGeometry(1.1, 1.1, 1.1);
  const outerWire = new THREE.LineSegments(new THREE.EdgesGeometry(outerGeo), new THREE.LineBasicMaterial({ color: 0x5c6370 }));
  const innerGeo = new THREE.IcosahedronGeometry(0.48, 0);
  const innerWire = new THREE.LineSegments(new THREE.EdgesGeometry(innerGeo), new THREE.LineBasicMaterial({ color: 0x8b919d }));
  placeholderGroup.add(outerWire, innerWire);
  placeholderGroup.position.set(0, 0.9, 0);
  placeholderGroup.visible = false;
  scene.add(placeholderGroup);

  window.addEventListener("resize", () => {
    if (!container.clientWidth || !container.clientHeight) return;
    camera.aspect = container.clientWidth / Math.max(1, container.clientHeight);
    camera.updateProjectionMatrix();
    renderer.setSize(container.clientWidth, container.clientHeight);
  });

  function animate() {
    requestAnimationFrame(animate);
    if (placeholderGroup && placeholderGroup.visible) {
      placeholderGroup.rotation.y += 0.012;
      placeholderGroup.children[1].rotation.x += 0.016;
    }
    if (currentRoot && window._autoSpinStage1) {
      currentRoot.rotation.y += 0.006;
    }
    controls.update();
    renderer.render(scene, camera);
  }
  animate();
}

function clearMeshes() {
  if (currentRoot) {
    scene.remove(currentRoot);
    currentRoot = null;
  }
  if (pbrMesh) {
    scene.remove(pbrMesh);
    pbrMesh = null;
  }
}

export function setPlaceholder3D(visible) {
  init3D();
  if (placeholderGroup) placeholderGroup.visible = !!visible;
  if (visible) clearMeshes();
}

export function loadGlbInViewer(url, autoSpin = false, crispPixels = true) {
  init3D();
  if (placeholderGroup) placeholderGroup.visible = false;
  window._autoSpinStage1 = !!autoSpin;
  clearMeshes();
  document.getElementById("pbrShapeBar")?.classList.add("hidden");
  if (!url) return;

  const loader = new GLTFLoader();
  loader.load(url + "?t=" + Date.now(), (gltf) => {
    currentRoot = gltf.scene;
    if (crispPixels) {
      currentRoot.traverse((obj) => {
        if (obj.isMesh && obj.material && obj.material.map) {
          obj.material.map.magFilter = THREE.NearestFilter;
          obj.material.map.minFilter = THREE.NearestFilter;
          obj.material.map.needsUpdate = true;
        }
      });
    }
    scene.add(currentRoot);
    const box = new THREE.Box3().setFromObject(currentRoot);
    const center = box.getCenter(new THREE.Vector3());
    controls.target.copy(center);
    camera.position.set(center.x + 2.2, center.y + 1.1, center.z + 2.6);
  });
}

export function loadPbrMaterialInViewer(urls, seamless = true, crispPixels = false) {
  init3D();
  if (placeholderGroup) placeholderGroup.visible = false;
  window._autoSpinStage1 = false;
  clearMeshes();
  lastPbrArgs = { urls, seamless, crispPixels };
  document.getElementById("pbrShapeBar")?.classList.remove("hidden");
  if (!urls || !urls.albedo) return;

  const texLoader = new THREE.TextureLoader();
  const bust = "?t=" + Date.now();
  const rep = seamless ? 2 : 1;

  const loadTex = (u, srgb = false) => {
    if (!u) return null;
    const t = texLoader.load(u + bust);
    t.wrapS = seamless ? THREE.RepeatWrapping : THREE.ClampToEdgeWrapping;
    t.wrapT = seamless ? THREE.RepeatWrapping : THREE.ClampToEdgeWrapping;
    t.repeat.set(rep, rep);
    if (crispPixels) {
      t.magFilter = THREE.NearestFilter;
      t.minFilter = THREE.NearestFilter;
    }
    if (srgb) t.colorSpace = THREE.SRGBColorSpace;
    return t;
  };

  const mat = new THREE.MeshStandardMaterial({
    map: loadTex(urls.albedo, true),
    normalMap: loadTex(urls.normal, false),
    roughnessMap: loadTex(urls.roughness, false),
    aoMap: loadTex(urls.ao, false),
    metalnessMap: loadTex(urls.metallic, false),
    roughness: 1.0,
    metalness: urls.metallic ? 0.85 : 0.05,
  });

  let geo;
  if (pbrShape === "cube") geo = new THREE.BoxGeometry(1.4, 1.4, 1.4);
  else if (pbrShape === "plane") geo = new THREE.PlaneGeometry(2.0, 2.0, 32, 32);
  else geo = new THREE.SphereGeometry(0.85, 48, 32);

  if (geo.attributes.uv && !geo.attributes.uv2) {
    geo.setAttribute("uv2", geo.attributes.uv);
  }

  pbrMesh = new THREE.Mesh(geo, mat);
  pbrMesh.position.set(0, 0.9, 0);
  if (pbrShape === "plane") pbrMesh.rotation.x = -Math.PI / 4;
  scene.add(pbrMesh);

  controls.target.set(0, 0.9, 0);
  camera.position.set(1.8, 1.7, 2.3);
}

window.setPbrShape = function(shape, btn) {
  pbrShape = shape;
  document.querySelectorAll("#pbrShapeBar .tab").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  if (lastPbrArgs) {
    loadPbrMaterialInViewer(lastPbrArgs.urls, lastPbrArgs.seamless, lastPbrArgs.crispPixels);
  }
};

window.setPlaceholder3D = setPlaceholder3D;
window.loadGlbInViewer = loadGlbInViewer;
window.loadPbrMaterialInViewer = loadPbrMaterialInViewer;

init3D();
