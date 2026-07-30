/*global document, window*/
/*
 * Las dos barras fijas de movil. El CSS decide DONDE viven y en que tamanos
 * existen; este fichero solo decide CUANDO se ven.
 *
 * Las dos son copias de un control que ya esta en la pagina, nunca un control
 * nuevo: el "volver" clona el enlace de la cabecera y el boton de compra envia
 * el mismo formulario que el boton original, con el atributo `form`. Asi no
 * pueden desincronizarse del destino ni de las cantidades que haya elegido el
 * comprador, y si este fichero no llega a cargarse la pagina se queda como
 * estaba: los controles originales siguen ahi y siguen funcionando.
 */
(function () {
    "use strict";

    var FORM_ID = "cart-add-form";
    var UMBRAL_SCROLL = 8;  // px; por debajo de esto es temblor de dedo, no intencion

    function alEstarLista(fn) {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", fn);
        } else {
            fn();
        }
    }

    /*
     * "Volver": se aparta al bajar y vuelve al subir. Arriba del todo no se
     * muestra nunca, porque ahi el enlace de verdad ya esta a la vista.
     */
    function montarVolver() {
        var original = document.querySelector("a.page-header-link-back");
        if (!original) {
            return;
        }

        var barra = document.createElement("div");
        barra.className = "sticky-back";
        var copia = original.cloneNode(true);
        copia.removeAttribute("id");
        barra.appendChild(copia);
        document.body.appendChild(barra);

        var ultimoY = window.pageYOffset;
        var pendiente = false;

        function evaluar() {
            pendiente = false;
            var y = window.pageYOffset;
            var delta = y - ultimoY;

            if (Math.abs(delta) < UMBRAL_SCROLL) {
                return;
            }
            // Cerca del principio el enlace original ya se ve: sobra la copia.
            var visible = delta < 0 && y > 120;
            barra.classList.toggle("is-visible", visible);
            ultimoY = y;
        }

        window.addEventListener("scroll", function () {
            if (!pendiente) {
                pendiente = true;
                window.requestAnimationFrame(evaluar);
            }
        }, {passive: true});
    }

    /*
     * "Comprar": aparece en cuanto el boton de verdad se va de la pantalla, sin
     * importar si el comprador aun no ha bajado hasta el o si ya lo ha pasado.
     * En los dos casos la accion deja de estar a un dedo de distancia, que es lo
     * unico que esta barra viene a arreglar.
     */
    function montarComprar() {
        var boton = document.getElementById("btn-add-to-cart");
        var formulario = document.getElementById(FORM_ID);
        if (!boton || !formulario || !("IntersectionObserver" in window)) {
            return;
        }

        var barra = document.createElement("div");
        barra.className = "sticky-buy";

        var copia = document.createElement("button");
        copia.type = "submit";
        copia.className = "btn btn-block btn-primary btn-lg";
        copia.setAttribute("form", FORM_ID);
        copia.innerHTML = boton.innerHTML;
        barra.appendChild(copia);

        document.body.appendChild(barra);
        document.body.classList.add("has-sticky-buy");

        new window.IntersectionObserver(function (entradas) {
            entradas.forEach(function (entrada) {
                barra.classList.toggle("is-visible", !entrada.isIntersecting);
            });
        }, {threshold: 0}).observe(boton);
    }

    alEstarLista(function () {
        montarVolver();
        montarComprar();
    });
}());
